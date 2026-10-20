"""LangGraph Node Implementations (PRD §4.1, FR-2.1 - FR-2.4)."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict

from scout.agents.planner import PlannerAgent
from scout.agents.researcher import ResearcherAgent
from scout.agents.writer import WriterAgent
from scout.config import get_settings
from scout.effects.fault_injection import maybe_crash
from scout.graph.budget_guard import check_budget_guard, check_minimum_evidence_for_report
from scout.graph.state import ResearchState
from scout.graph.supervisor import SupervisorReviewer
from scout.validate.citation_validator import validate_report_citations
from scout.validate.markdown_renderer import render_markdown_report

logger = logging.getLogger(__name__)


def init_run_node(state: ResearchState) -> Dict[str, Any]:
    """Initializes the research run, setting up wall-clock deadlines and defaults."""
    settings = get_settings()
    default_budgets = settings.budgets["default"]

    wall_clock_s = state.options.get("wall_clock_s") or default_budgets.wall_clock_s
    deadline_ts = time.time() + wall_clock_s

    logger.info("Initializing run '%s' with deadline in %ds", state.run_id, wall_clock_s)
    return {
        "status": "running",
        "started_at_ts": time.time(),
        "deadline_ts": deadline_ts,
    }


def plan_node(state: ResearchState, planner_agent: PlannerAgent) -> Dict[str, Any]:
    """Decomposes the main research question into structured sub-questions."""
    if state.cancellation_requested:
        return {"status": "cancelled", "termination_reason": "cancelled"}

    settings = get_settings()
    max_subq = state.options.get("max_subquestions") or settings.budgets["default"].max_subquestions

    logger.info("Generating research plan for question: '%s'", state.question)
    plan = planner_agent.plan(state.question, max_subquestions=max_subq)

    # Update ledger for planner LLM call
    ledger = state.budget_ledger
    ledger.llm_calls += 1

    pending_ids = [sq.id for sq in plan.sub_questions]
    return {
        "plan": plan,
        "pending_subquestion_ids": pending_ids,
        "budget_ledger": ledger,
    }


def pick_next_node(state: ResearchState) -> Dict[str, Any]:
    """Picks the next pending sub-question or transitions toward synthesis."""
    if state.cancellation_requested:
        return {"status": "cancelled", "termination_reason": "cancelled"}

    can_proceed, reason = check_budget_guard(state)
    if not can_proceed:
        logger.warning("Budget guard prevented new work dispatch: %s", reason)
        return {"termination_reason": reason}

    if not state.pending_subquestion_ids:
        logger.info("No more pending sub-questions in queue.")
        return {"active_subquestion_id": None}

    next_id = state.pending_subquestion_ids[0]
    remaining = state.pending_subquestion_ids[1:]

    logger.info("Picked next active sub-question: '%s'", next_id)
    return {
        "active_subquestion_id": next_id,
        "pending_subquestion_ids": remaining,
    }


def research_node(state: ResearchState, researcher_agent: ResearcherAgent) -> Dict[str, Any]:
    """Executes search, fetch, and extraction on the active sub-question."""
    if state.cancellation_requested:
        return {"status": "cancelled", "termination_reason": "cancelled"}

    active_sq = state.get_active_subquestion()
    if not active_sq:
        return {}

    logger.info("Executing research for sub-question '%s': %s", active_sq.id, active_sq.question)
    result, new_findings = researcher_agent.execute_subquestion(active_sq)

    ledger = state.budget_ledger
    ledger.searches += result.searches_used
    ledger.fetches += result.pages_fetched
    ledger.llm_calls += result.llm_calls_used

    return {
        "findings": new_findings,
        "failures": result.failures,
        "budget_ledger": ledger,
    }


def review_node(state: ResearchState, supervisor_reviewer: SupervisorReviewer) -> Dict[str, Any]:
    """Reviews the research output for the active sub-question."""
    active_sq = state.get_active_subquestion()
    if not active_sq:
        return {}

    # Extract findings belonging to this sub-question
    sq_findings = [f for f in state.findings if f.subquestion_id == active_sq.id]
    decision = supervisor_reviewer.review(active_sq, sq_findings, state)
    logger.info("Supervisor decision for '%s': %s (mode: %s)", active_sq.id, decision.decision, decision.mode)

    completed = list(state.completed_subquestion_ids)
    abandoned = list(state.abandoned_subquestion_ids)
    pending = list(state.pending_subquestion_ids)
    rework_counts = dict(state.rework_counts)
    total_reworks = state.total_reworks

    if decision.decision == "accept":
        completed.append(active_sq.id)
    elif decision.decision == "abandon":
        abandoned.append(active_sq.id)
    elif decision.decision == "rework":
        rework_counts[active_sq.id] = rework_counts.get(active_sq.id, 0) + 1
        total_reworks += 1
        pending.insert(0, active_sq.id)  # re-queue for immediate rework pass

    return {
        "completed_subquestion_ids": completed,
        "abandoned_subquestion_ids": abandoned,
        "pending_subquestion_ids": pending,
        "rework_counts": rework_counts,
        "total_reworks": total_reworks,
        "active_subquestion_id": None,
    }


def write_node(state: ResearchState, writer_agent: WriterAgent) -> Dict[str, Any]:
    """Dispatches the Writer agent to synthesize accepted findings into a cited report."""
    if state.cancellation_requested:
        maybe_crash("during_cancellation")
        return {"status": "cancelled", "termination_reason": "cancelled"}

    maybe_crash("during_writing")
    accepted_findings = state.get_accepted_findings()
    logger.info("Writer synthesizing report with %d verified findings", len(accepted_findings))

    # Pass feedback if regenerating after a failed validation attempt (PRD FR-5.4)
    validation_feedback = None
    if state.validation_result and not state.validation_result.valid:
        validation_feedback = "\n".join(state.validation_result.unresolved_citations)

    report = writer_agent.write(
        question=state.question,
        plan=state.plan,
        findings=accepted_findings,
        abandoned_subquestions=state.abandoned_subquestion_ids,
        validation_feedback=validation_feedback,
    )

    ledger = state.budget_ledger
    ledger.llm_calls += 1

    return {
        "report": report,
        "budget_ledger": ledger,
    }


def validate_citations_node(state: ResearchState) -> Dict[str, Any]:
    """Mechanically checks citation resolution, handles bounded regenerations, and re-checks evidence."""
    maybe_crash("during_validation")
    if not state.report:
        return {"status": "failed", "termination_reason": "missing_report"}

    findings_registry = {f.finding_id: f for f in state.get_accepted_findings()}
    validation = validate_report_citations(state.report, findings_registry)
    logger.info("Citation validation result: valid=%s, unresolved=%s", validation.valid, validation.unresolved_citations)

    settings = get_settings()
    max_regenerations = settings.budgets["default"].writer_regenerations

    if validation.valid:
        return {"validation_result": validation}

    current_regenerations = state.writer_regenerations
    if current_regenerations < max_regenerations:
        logger.warning(
            "Citation resolution failed (attempt %d/%d). Triggering Writer regeneration with feedback.",
            current_regenerations + 1,
            max_regenerations,
        )
        return {
            "validation_result": validation,
            "writer_regenerations": current_regenerations + 1,
        }

    # Max regenerations exhausted: drop unresolved claims & re-check minimum evidence (PRD FR-5.4, FR-2.4)
    logger.warning("Writer regenerations exhausted. Dropping unverified claims.")
    for section in state.report.sections:
        section.claims = [c for c in section.claims if c not in validation.dropped_claims]

    plan_len = len(state.plan.sub_questions) if state.plan else 2
    evidence_ok, reason = check_minimum_evidence_for_report(
        state.report,
        findings_registry,
        plan_length=plan_len,
    )

    if not evidence_ok:
        logger.warning("Pruned report failed minimum evidence re-check: %s", reason)
        return {
            "status": "failed",
            "termination_reason": "insufficient_evidence",
            "validation_result": validation,
            "report": None,
        }

    # Re-render markdown from validated structure without unresolved citations
    state.report.markdown = render_markdown_report(
        title=state.report.title,
        summary=state.report.summary,
        sections=state.report.sections,
        findings_registry=findings_registry,
        coverage_gaps=state.report.coverage_gaps,
        dropped_claims=validation.dropped_claims,
    )

    return {
        "validation_result": validation,
        "report": state.report,
    }


def finalize_node(state: ResearchState) -> Dict[str, Any]:
    """Finalizes successful or partial research run."""
    has_abandoned = len(state.abandoned_subquestion_ids) > 0
    has_budget_stopped = state.termination_reason is not None
    had_dropped_claims = bool(state.validation_result and state.validation_result.dropped_claims)

    status = "completed_partial" if (has_abandoned or has_budget_stopped or had_dropped_claims) else "completed"
    logger.info("Run '%s' finalized as '%s'", state.run_id, status)
    return {
        "status": status,
    }


def finalize_fail_node(state: ResearchState) -> Dict[str, Any]:
    """Finalizes failed or cancelled run with structured diagnostic envelope (PRD FR-2.4)."""
    reason = state.termination_reason or "insufficient_evidence"
    status = "cancelled" if reason == "cancelled" else "failed"
    logger.warning("Run '%s' terminated as '%s' with reason: %s", state.run_id, status, reason)

    diagnostics = {
        "termination_reason": reason,
        "question": state.question,
        "completed_subquestions": state.completed_subquestion_ids,
        "abandoned_subquestions": state.abandoned_subquestion_ids,
        "total_failures": len(state.failures),
        "failures": [f.model_dump() for f in state.failures],
        "budget_ledger": state.budget_ledger.model_dump(),
    }

    return {
        "status": status,
        "termination_reason": reason,
        "report": None,
        "diagnostics": diagnostics,
    }
