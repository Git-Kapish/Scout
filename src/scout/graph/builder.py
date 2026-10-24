"""LangGraph Graph Assembly and Conditional Routing (PRD §4.1, FR-2.1).

Builds the research topology:
init_run -> plan -> pick_next -> research -> review -> pick_next
                             -> write -> validate_citations -> finalize / finalize_fail
"""

from __future__ import annotations

from typing import Literal, Optional

from langgraph.graph import END, START, StateGraph

from scout.agents.planner import PlannerAgent
from scout.agents.researcher import ResearcherAgent
from scout.agents.writer import WriterAgent
from scout.config import get_settings
from scout.graph.budget_guard import check_minimum_evidence
from scout.graph.nodes import (
    finalize_fail_node,
    finalize_node,
    init_run_node,
    pick_next_node,
    plan_node,
    research_node,
    review_node,
    validate_citations_node,
    write_node,
)
from scout.graph.state import ResearchState
from scout.graph.supervisor import SupervisorReviewer


def route_after_pick_next(
    state: ResearchState,
) -> Literal["research", "write", "finalize_fail"]:
    """Conditional edge from pick_next: dispatches research, transitions to writing, or fails."""
    if state.status == "cancelled" or state.cancellation_requested:
        return "finalize_fail"

    # If an active sub-question was selected, proceed to research
    if state.active_subquestion_id:
        return "research"

    # Queue is empty OR budget guard halted dispatch: check minimum evidence (PRD FR-2.4)
    is_sufficient, reason = check_minimum_evidence(state)
    if is_sufficient:
        return "write"

    import logging
    logging.getLogger(__name__).warning("Minimum evidence check failed: %s", reason)
    return "finalize_fail"


def route_after_validation(
    state: ResearchState,
) -> Literal["write", "finalize", "finalize_fail"]:
    """Conditional edge from validate_citations: handles regenerations and claim stripping."""
    if state.status in ("failed", "cancelled") or state.termination_reason == "insufficient_evidence":
        return "finalize_fail"

    if not state.validation_result:
        return "finalize_fail"

    if state.validation_result.valid:
        return "finalize"

    settings = get_settings()
    max_regenerations = settings.budgets["default"].writer_regenerations

    # If within regeneration budget, retry write
    if state.writer_regenerations <= max_regenerations and state.report is not None:
        return "write"

    # Regenerations exhausted: claims were pruned and evidence checked in validate_citations_node
    if state.report is not None:
        return "finalize"

    return "finalize_fail"



def create_research_graph(
    planner_agent: Optional[PlannerAgent] = None,
    researcher_agent: Optional[ResearcherAgent] = None,
    writer_agent: Optional[WriterAgent] = None,
    supervisor_reviewer: Optional[SupervisorReviewer] = None,
    checkpointer=None,
):
    """Assembles and compiles the LangGraph research state machine."""
    planner = planner_agent or PlannerAgent()
    researcher = researcher_agent or ResearcherAgent()
    writer = writer_agent or WriterAgent()
    supervisor = supervisor_reviewer or SupervisorReviewer()

    workflow = StateGraph(ResearchState)

    # Register Nodes
    workflow.add_node("init_run", init_run_node)
    workflow.add_node("plan", lambda s: plan_node(s, planner))
    workflow.add_node("pick_next", pick_next_node)
    workflow.add_node("research", lambda s: research_node(s, researcher))
    workflow.add_node("review", lambda s: review_node(s, supervisor))
    workflow.add_node("write", lambda s: write_node(s, writer))
    workflow.add_node("validate_citations", validate_citations_node)
    workflow.add_node("finalize", finalize_node)
    workflow.add_node("finalize_fail", finalize_fail_node)

    # Core Edges
    workflow.add_edge(START, "init_run")
    workflow.add_edge("init_run", "plan")
    workflow.add_edge("plan", "pick_next")

    # Pick Next Conditional Edge
    workflow.add_conditional_edges(
        "pick_next",
        route_after_pick_next,
        {
            "research": "research",
            "write": "write",
            "finalize_fail": "finalize_fail",
        },
    )

    # Research Loop Edges
    workflow.add_edge("research", "review")
    workflow.add_edge("review", "pick_next")

    # Writing & Validation Edges
    workflow.add_edge("write", "validate_citations")
    workflow.add_conditional_edges(
        "validate_citations",
        route_after_validation,
        {
            "write": "write",
            "finalize": "finalize",
            "finalize_fail": "finalize_fail",
        },
    )

    # Terminal Edges
    workflow.add_edge("finalize", END)
    workflow.add_edge("finalize_fail", END)

    return workflow.compile(checkpointer=checkpointer)
