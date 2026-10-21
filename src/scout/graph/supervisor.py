"""Supervisor Review Logic (PRD FR-2.3).

Implements deterministic-first review routing:
Checks sufficiency signals (finding count, domain diversity, recency, failures)
before falling back to LLM evaluation when ambiguous.
Enforces at most 1 rework per sub-question and at most max_rework_total per run.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from scout.config import get_settings
from scout.effects.recorder import ReplayMissError
from scout.graph.state import ResearchState
from scout.llm import LLMClient
from scout.schemas import Finding, ReviewDecision, SubQuestion

logger = logging.getLogger(__name__)

SUPERVISOR_REVIEW_PROMPT = """You are a research supervisor evaluating research findings for a sub-question.

Sub-Question: {question}
Needs Recency: {needs_recency}

FINDINGS FOUND:
{findings_text}

FAILURES:
{failures_text}

Rules:
1. Decision must be 'accept', 'rework', or 'abandon'.
2. If findings sufficiently answer the sub-question, choose 'accept'.
3. If findings are missing key aspects or off-topic, and can be improved with a reformulated query, choose 'rework' with a 'reformulation_hint' and 'missing' topics.
4. If the source material does not exist or all attempts failed, choose 'abandon'.
5. Output ONLY valid JSON matching the ReviewDecision schema.
"""


class SupervisorReviewer:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()
        self.settings = get_settings()

    def review(
        self,
        sub_question: SubQuestion,
        subq_findings: List[Finding],
        state: ResearchState,
    ) -> ReviewDecision:
        review_mode = state.options.get("review_mode") or self.settings.review.mode

        # Check rework caps (PRD FR-2.3: at most 1 rework per sub-q, at most max_reworks total)
        current_sq_reworks = state.rework_counts.get(sub_question.id, 0)
        can_rework = (current_sq_reworks < 1) and (
            state.total_reworks < self.settings.budgets["default"].reworks_per_run
        )

        # Deterministic-First Review Checks
        if review_mode == "deterministic_first":
            verified_count = len([f for f in subq_findings if f.verified])
            distinct_domains = len({f.domain for f in subq_findings if f.verified and f.domain})

            # Clear Pass: 2+ verified findings from 1+ distinct domain
            if verified_count >= 2 and distinct_domains >= 1:
                return ReviewDecision(
                    decision="accept",
                    missing=[],
                    reason="Deterministic pass: 2+ verified findings from distinct domains.",
                    mode="deterministic_first",
                )

            # Clear Fail: 0 findings
            if verified_count == 0:
                if can_rework:
                    return ReviewDecision(
                        decision="rework",
                        missing=["all"],
                        reformulation_hint=f"Broaden query terms for {sub_question.question}",
                        reason="Deterministic rework: zero findings extracted on initial pass.",
                        mode="deterministic_first",
                    )
                else:
                    return ReviewDecision(
                        decision="abandon",
                        missing=["all"],
                        reason="Deterministic abandon: zero findings and rework budget exhausted.",
                        mode="deterministic_first",
                    )

            # Single finding: ambiguous, or proceed to LLM review if configured
            # If 1 finding and no recency required, accept to conserve quota
            if verified_count == 1 and not sub_question.needs_recency:
                return ReviewDecision(
                    decision="accept",
                    missing=[],
                    reason="Deterministic pass: single verified finding satisfies non-recency query.",
                    mode="deterministic_first",
                )

        # LLM Review (either review_mode='llm' or ambiguous in deterministic_first)
        findings_lines = [f"- {f.claim} (from {f.domain})" for f in subq_findings if f.verified]
        findings_text = "\n".join(findings_lines) if findings_lines else "None."

        failures_lines = [f"- {fail.tool}: {fail.error_message}" for fail in state.failures if fail.target]
        failures_text = "\n".join(failures_lines) if failures_lines else "None."

        prompt = SUPERVISOR_REVIEW_PROMPT.format(
            question=sub_question.question,
            needs_recency=sub_question.needs_recency,
            findings_text=findings_text,
            failures_text=failures_text,
        )

        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Provide your review decision."},
        ]

        try:
            resp = self.llm_client.generate(
                role="supervisor",
                messages=messages,
                response_model=ReviewDecision,
            )
            decision: ReviewDecision = resp.parsed
            decision.mode = "llm"

            # Enforce rework clamp if LLM chose rework but budget is exhausted
            if decision.decision == "rework" and not can_rework:
                if subq_findings:
                    decision.decision = "accept"
                    decision.reason += " (Rework clamped: rework limit reached, accepted partial evidence)."
                else:
                    decision.decision = "abandon"
                    decision.reason += " (Rework clamped: rework limit reached, abandoned sub-question)."

            return decision

        except ReplayMissError:
            raise
        except Exception as e:
            # Budget/cancel stops land here too: the deterministic fallback needs no LLM call.
            logger.error("Supervisor LLM review failed: %s. Falling back to deterministic decision.", e)
            if subq_findings:
                return ReviewDecision(
                    decision="accept",
                    reason="Fallback accept after review exception.",
                    mode="deterministic_first",
                )
            return ReviewDecision(
                decision="abandon" if not can_rework else "rework",
                reason="Fallback decision after review exception.",
                mode="deterministic_first",
            )
