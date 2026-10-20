"""Data Models for Ordered Step Trace and Redacted Logging (PRD FR-5.5, §8)."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


StepKind = Literal["llm", "tool", "decision", "validation"]
StepStatus = Literal["ok", "partial", "error", "cancelled"]


class TracePayload(BaseModel):
    """Payload representation with reference and 2 KB preview (PRD FR-5.5)."""
    reference: Optional[str] = None
    preview: str = ""
    full: Optional[Any] = None


class ReportedUsage(BaseModel):
    """Usage statistics reported by providers or models with confidence markers."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False
    unknown: bool = False


class TraceStep(BaseModel):
    """Canonical step log entry for the research run trace (PRD FR-5.5)."""
    step_id: str = Field(default_factory=lambda: f"step_{uuid.uuid4().hex[:12]}")
    run_id: str
    timestamp: float = Field(default_factory=time.time)
    agent_node: str
    step_kind: StepKind
    received_input: TracePayload = Field(default_factory=TracePayload)
    produced_output: TracePayload = Field(default_factory=TracePayload)
    tool_name: Optional[str] = None
    tool_args: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    token_reservation: Optional[int] = None
    reported_usage: Optional[ReportedUsage] = None
    latency_ms: float = 0.0
    retry_resume_markers: bool = False
    cache_hit: bool = False
    effect_id: Optional[str] = None
    effect_state: Optional[str] = None
    fencing_generation: int = 0
    status: StepStatus = "ok"
    error: Optional[str] = None


class ResearchTraceResponse(BaseModel):
    """API Response contract for GET /research/{id}/trace (PRD §8, FR-5.5)."""
    run_id: str
    total_steps: int
    steps: List[TraceStep] = Field(default_factory=list)
