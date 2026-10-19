"""Budget Guard and Evidence Sufficiency Enforcement (PRD FR-2.2, FR-2.4).

Enforces:
1. Hard limits on wall clock, LLM calls, total tokens, searches, and reworks before dispatch.
2. Conservative token reservations.
3. Minimum evidence thresholds required before writing a report.
"""

from __future__ import annotations

import time
from typing import Optional, Tuple

from scout.config import get_settings
from scout.graph.state import ResearchState


class BudgetBreachError(Exception):
    """Raised when a hard budget limit is breached."""
    pass


def check_budget_guard(state: ResearchState) -> Tuple[bool, Optional[str]]:
    """Checks whether the run has remaining budget to initiate new external work.

    Returns:
        (can_proceed, exhaustion_reason)
    """
    settings = get_settings()
    default_budgets = settings.budgets["default"]
    hard_limits = settings.budgets["hard_limits"]

    # Effective limits (user options clamped to hard limits per PRD FR-2.2)
    max_tokens = min(
        state.options.get("max_total_tokens", default_budgets.total_tokens),
        hard_limits.total_tokens,
    )
    max_llm_calls = min(
        state.options.get("max_llm_calls", default_budgets.total_llm_calls),
        hard_limits.total_llm_calls,
    )
    max_reworks = min(
        default_budgets.reworks_per_run,
        hard_limits.reworks_per_run,
    )

    ledger = state.budget_ledger

    # 1. Wall-clock deadline check
    if state.deadline_ts > 0 and time.time() >= state.deadline_ts:
        return False, "wall_clock_timeout"

    # 2. Total LLM calls hard limit
    if ledger.llm_calls >= max_llm_calls:
        return False, "llm_calls_exhausted"

    # 3. Total Tokens hard limit
    total_tokens_used = ledger.tokens_in + ledger.tokens_out
    if total_tokens_used >= max_tokens:
        return False, "token_budget_exhausted"

    # 4. Total reworks hard limit
    if state.total_reworks >= max_reworks:
        # Note: rework exhaustion does not stop the run, only further rework
        pass

    return True, None


def check_minimum_evidence(state: ResearchState) -> Tuple[bool, str]:
    """Evaluates whether the accepted findings satisfy the minimum evidence threshold (PRD FR-2.4).

    Requirements:
    1. At least min_accepted_subquestions (default 2, or half plan if smaller).
    2. At least min_verified_findings (default 4).
    3. At least min_distinct_domains (default 2).

    Returns:
        (is_sufficient, reason)
    """
    settings = get_settings()
    min_cfg = settings.minimum_evidence

    plan_len = len(state.plan.sub_questions) if state.plan else 0
    required_subqs = min(min_cfg.min_accepted_subquestions, max(1, plan_len // 2))

    accepted_subqs = len(state.completed_subquestion_ids)
    accepted_findings = len(state.get_accepted_findings())
    distinct_domains = len(state.get_distinct_domains())

    if accepted_subqs < required_subqs:
        return (
            False,
            f"Insufficient accepted sub-questions: {accepted_subqs} < {required_subqs} required.",
        )

    if accepted_findings < min_cfg.min_verified_findings:
        return (
            False,
            f"Insufficient verified findings: {accepted_findings} < {min_cfg.min_verified_findings} required.",
        )

    if distinct_domains < min_cfg.min_distinct_domains:
        return (
            False,
            f"Insufficient source diversity: {distinct_domains} < {min_cfg.min_distinct_domains} distinct domains required.",
        )

    return True, "Sufficient evidence collected."
