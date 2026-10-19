"""Scout Typed Pydantic Contracts (PRD §9.1, FR-1.1, FR-3.4).

All inter-agent communication and persistence hand-offs occur exclusively
through these typed models.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Closed status enum for research & tool execution (PRD FR-3.4)
ResearchStatus = Literal[
    "ok",
    "partial",
    "no_results",
    "rate_limited",
    "paywalled",
    "blocked",
    "timeout",
    "budget_exhausted",
    "error",
]


class SubQuestion(BaseModel):
    id: str = Field(description="Unique identifier, e.g. 'sq_1'")
    question: str = Field(description="Self-contained research question")
    rationale: str = Field(description="Why this sub-question is needed to answer the main prompt")
    priority: int = Field(default=1, ge=1, le=5, description="1 (highest) to 5 (lowest)")
    needs_recency: bool = Field(default=False, description="Whether recent sources are required")
    expected_source_types: List[str] = Field(
        default_factory=lambda: ["article", "documentation", "report"]
    )


class Plan(BaseModel):
    schema_version: str = "v1"
    sub_questions: List[SubQuestion] = Field(default_factory=list)

    @field_validator("sub_questions")
    @classmethod
    def validate_unique_ids(cls, v: List[SubQuestion]) -> List[SubQuestion]:
        ids = [sq.id for sq in v]
        if len(ids) != len(set(ids)):
            raise ValueError(f"Duplicate sub-question IDs found: {ids}")
        return v


class Finding(BaseModel):
    finding_id: str = Field(description="Unique identifier, e.g. 'find_abc123'")
    subquestion_id: str = Field(description="Associated sub-question ID")
    claim: str = Field(description="The factual claim extracted by the LLM")
    snippet: str = Field(description="Verbatim text quote from the page verifying the claim")
    source_url: str = Field(description="Canonical source URL")
    domain: str = Field(description="Registered domain name")
    title: Optional[str] = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    page_hash: str = Field(description="SHA256 hash of raw page content")
    verified: bool = Field(default=False, description="Whether snippet was verified in page text")


class ToolFailure(BaseModel):
    tool: str = Field(description="Tool name: 'search', 'fetch', or 'extract'")
    status: ResearchStatus
    error_message: str
    target: Optional[str] = None  # URL or query string


class ResearchResult(BaseModel):
    subquestion_id: str
    status: ResearchStatus
    finding_ids: List[str] = Field(default_factory=list)
    searches_used: int = 0
    pages_fetched: int = 0
    failures: List[ToolFailure] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    decision: Literal["accept", "rework", "abandon"]
    missing: List[str] = Field(default_factory=list)
    reformulation_hint: Optional[str] = None
    reason: str
    mode: Literal["deterministic_first", "llm"] = "deterministic_first"


class Claim(BaseModel):
    text: str = Field(description="Statement of fact in the report")
    finding_ids: List[str] = Field(
        min_length=1,
        description="At least one verified finding_id citation supporting this claim",
    )


class ReportSection(BaseModel):
    title: str
    content: str
    claims: List[Claim] = Field(default_factory=list)


class Report(BaseModel):
    schema_version: str = "v1"
    title: str
    summary: str
    sections: List[ReportSection] = Field(default_factory=list)
    coverage_gaps: List[str] = Field(default_factory=list)
    markdown: Optional[str] = None


class BudgetLedger(BaseModel):
    tokens_in: int = 0
    tokens_out: int = 0
    llm_calls: int = 0
    searches: int = 0
    fetches: int = 0
    search_credits: float = 0.0
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    estimated_token_fraction: float = 0.0


class ValidationResult(BaseModel):
    valid: bool
    unresolved_citations: List[str] = Field(default_factory=list)
    missing_sources: List[str] = Field(default_factory=list)
    dropped_claims: List[Claim] = Field(default_factory=list)
    details: str = ""
