"""Pydantic v2 schemas for the Scout Research API (PRD §8, FR-5.1 - FR-5.3)."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ResearchOptions(BaseModel):
    max_subquestions: Optional[int] = Field(None, ge=1, le=8)
    max_total_tokens: Optional[int] = Field(None, ge=1000, le=300000)
    wall_clock_s: Optional[int] = Field(None, ge=30, le=1800)
    review_mode: Optional[Literal["deterministic_first", "llm"]] = "deterministic_first"


class ResearchRequest(BaseModel):
    question: str = Field(..., min_length=3, description="Research question to investigate")
    options: Optional[ResearchOptions] = Field(default_factory=ResearchOptions)


class ResearchAcceptedLinks(BaseModel):
    status: str
    events: str
    trace: str


class ResearchAcceptedResponse(BaseModel):
    run_id: str
    status: str = "queued"
    links: ResearchAcceptedLinks


class AdmissionErrorResponse(BaseModel):
    code: str = "daily_allowance"
    retry_after_s: int
    projected_calls: int
    remaining_calls: int


class ProgressInfo(BaseModel):
    subquestions_total: int = 0
    subquestions_done: int = 0


class BudgetInfo(BaseModel):
    tokens_used: int = 0
    tokens_max: int = 150000
    llm_calls: int = 0
    searches: int = 0
    elapsed_s: float = 0.0


class ClaimData(BaseModel):
    text: str
    citations: List[str] = Field(default_factory=list)


class SourceData(BaseModel):
    label: str
    url: str
    title: Optional[str] = None
    retrieved_at: Optional[str] = None


class ReportData(BaseModel):
    markdown: str = ""
    claims: List[ClaimData] = Field(default_factory=list)
    sources: List[SourceData] = Field(default_factory=list)
    coverage_gaps: List[str] = Field(default_factory=list)
    claim_support_status: str = "not_evaluated"


class ResearchStatusResponse(BaseModel):
    run_id: str
    status: str
    question: str
    progress: ProgressInfo
    budget: BudgetInfo
    report: Optional[ReportData] = None
    termination_reason: Optional[str] = None


class ActionResponse(BaseModel):
    run_id: str
    status: str
    message: Optional[str] = None


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    details: Optional[Dict[str, Any]] = None
