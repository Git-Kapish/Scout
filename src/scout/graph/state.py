"""ResearchState Typed Graph State (PRD §4.1, FR-2.1).

Single source of truth for the LangGraph state machine.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from scout.schemas import (
    BudgetLedger,
    Finding,
    Plan,
    Report,
    SubQuestion,
    ToolFailure,
    ValidationResult,
)


def merge_findings(existing: List[Finding], new_items: List[Finding]) -> List[Finding]:
    """Reducer for findings: appends new findings avoiding ID duplicates."""
    seen = {f.finding_id for f in existing}
    res = list(existing)
    for f in new_items:
        if f.finding_id not in seen:
            seen.add(f.finding_id)
            res.append(f)
    return res


def merge_failures(existing: List[ToolFailure], new_items: List[ToolFailure]) -> List[ToolFailure]:
    """Reducer for failures: appends tool failures."""
    return existing + new_items


class ResearchState(BaseModel):
    run_id: str
    question: str
    options: Dict[str, Any] = Field(default_factory=dict)
    status: Literal["running", "completed", "completed_partial", "failed", "cancelled"] = "running"
    termination_reason: Optional[str] = None

    # Planning
    plan: Optional[Plan] = None
    active_subquestion_id: Optional[str] = None
    pending_subquestion_ids: List[str] = Field(default_factory=list)
    completed_subquestion_ids: List[str] = Field(default_factory=list)
    abandoned_subquestion_ids: List[str] = Field(default_factory=list)

    # Rework & Regeneration counters
    rework_counts: Dict[str, int] = Field(default_factory=dict)
    total_reworks: int = 0
    writer_regenerations: int = 0

    # Accumulators with reducers
    findings: Annotated[List[Finding], merge_findings] = Field(default_factory=list)
    failures: Annotated[List[ToolFailure], merge_failures] = Field(default_factory=list)

    # Budgets & Accounting
    budget_ledger: BudgetLedger = Field(default_factory=BudgetLedger)
    started_at_ts: float = Field(default_factory=time.time)
    deadline_ts: float = 0.0

    # Output
    report: Optional[Report] = None
    validation_result: Optional[ValidationResult] = None
    diagnostics: Dict[str, Any] = Field(default_factory=dict)

    # Control flags
    cancellation_requested: bool = False
    budget_warning_emitted: bool = False

    def get_active_subquestion(self) -> Optional[SubQuestion]:
        if not self.plan or not self.active_subquestion_id:
            return None
        for sq in self.plan.sub_questions:
            if sq.id == self.active_subquestion_id:
                return sq
        return None

    def get_accepted_findings(self) -> List[Finding]:
        return [f for f in self.findings if f.verified]

    def get_distinct_domains(self) -> List[str]:
        return list({f.domain for f in self.findings if f.verified and f.domain})
