"""ResearchState Typed Graph State (PRD §4.1, FR-2.1).

Single source of truth for the LangGraph state machine.
"""

from __future__ import annotations

import time
from typing import Annotated, Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from scout.schemas import (
    BudgetLedger,
    Finding,
    Plan,
    Report,
    SubQuestion,
    ToolFailure,
    ValidationResult,
)


def clean_lc_dict(val: Any) -> Any:
    """Recursively unwraps LangChain constructor serialization dictionaries."""
    if isinstance(val, dict):
        if val.get("lc") == 2 and "kwargs" in val:
            return clean_lc_dict(val["kwargs"])
        return {k: clean_lc_dict(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [clean_lc_dict(item) for item in val]
    return val


def _get_finding_id(item: Any) -> str:
    if isinstance(item, Finding):
        return item.finding_id
    elif isinstance(item, dict):
        if item.get("lc") == 2 and "kwargs" in item:
            return str(item["kwargs"].get("finding_id", ""))
        return str(item.get("finding_id", ""))
    return getattr(item, "finding_id", "")


def _ensure_finding(item: Any) -> Finding:
    if isinstance(item, Finding):
        return item
    elif isinstance(item, dict):
        cleaned = clean_lc_dict(item)
        return Finding.model_validate(cleaned)
    return item


def _ensure_failure(item: Any) -> ToolFailure:
    if isinstance(item, ToolFailure):
        return item
    elif isinstance(item, dict):
        cleaned = clean_lc_dict(item)
        return ToolFailure.model_validate(cleaned)
    return item


def merge_findings(existing: List[Any], new_items: List[Any]) -> List[Finding]:
    """Reducer for findings: appends new findings avoiding ID duplicates."""
    seen = {_get_finding_id(f) for f in existing}
    res = [_ensure_finding(f) for f in existing]
    for item in new_items:
        fid = _get_finding_id(item)
        if fid and fid not in seen:
            seen.add(fid)
            res.append(_ensure_finding(item))
    return res


def merge_failures(existing: List[Any], new_items: List[Any]) -> List[ToolFailure]:
    """Reducer for failures: appends tool failures."""
    return [_ensure_failure(f) for f in existing] + [_ensure_failure(f) for f in new_items]


class ResearchState(BaseModel):
    run_id: str = ""
    question: str = ""
    options: Dict[str, Any] = Field(default_factory=dict)
    status: Literal["running", "completed", "completed_partial", "failed", "cancelled"] = "running"
    termination_reason: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def unwrap_lc(cls, data: Any) -> Any:
        return clean_lc_dict(data)

    # Planning
    plan: Optional[Plan] = None
    active_subquestion_id: Optional[str] = None
    pending_subquestion_ids: List[str] = Field(default_factory=list)
    completed_subquestion_ids: List[str] = Field(default_factory=list)
    abandoned_subquestion_ids: List[str] = Field(default_factory=list)

    # Rework & Regeneration counters
    rework_counts: Dict[str, int] = Field(default_factory=dict)
    rework_hints: Dict[str, str] = Field(default_factory=dict)
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
        """Verified findings from accepted sub-questions only (PRD FR-2.4)."""
        accepted = set(self.completed_subquestion_ids)
        return [f for f in self.findings if f.verified and f.subquestion_id in accepted]

    def get_distinct_domains(self) -> List[str]:
        return list({f.domain for f in self.get_accepted_findings() if f.domain})
