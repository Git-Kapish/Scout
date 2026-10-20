"""Trace Store and Reconciler for Scout Research (PRD FR-5.5, §8, §13).

Provides:
- Append-only ordered step logging via Redis Streams (key_run_trace).
- Redacted payload formatting and 2 KB preview generation.
- Full payload inlining (?include=payloads).
- Acceptance criteria verification: trace-equals-ledger reconciliation.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import redis

from scout.effects.journal import EffectJournal, EffectRecord
from scout.store.redis_keys import key_run_effects, key_run_trace
from scout.trace.models import ReportedUsage, StepKind, StepStatus, TraceStep
from scout.trace.redaction import create_trace_payload, redact_payload

logger = logging.getLogger(__name__)


class TraceStore:
    """Manages ordered trace step recording, retrieval, and effect ledger reconciliation."""

    def __init__(self, r: redis.Redis):
        self.r = r

    def record_step(
        self,
        run_id: str,
        step: TraceStep,
        max_stream_len: int = 5000,
    ) -> str:
        """Appends a validated, redacted step entry to the run's trace stream."""
        trace_k = key_run_trace(run_id)

        # Ensure payloads are redacted
        if step.tool_args:
            step.tool_args = redact_payload(step.tool_args)

        # Serialize step data for stream storage
        step_data = {
            "step_id": step.step_id,
            "run_id": step.run_id,
            "timestamp": str(step.timestamp),
            "agent_node": step.agent_node,
            "step_kind": step.step_kind,
            "tool_name": step.tool_name or "",
            "provider": step.provider or "",
            "model": step.model or "",
            "token_reservation": str(step.token_reservation or 0),
            "latency_ms": str(step.latency_ms),
            "retry_resume_markers": "1" if step.retry_resume_markers else "0",
            "cache_hit": "1" if step.cache_hit else "0",
            "effect_id": step.effect_id or "",
            "effect_state": step.effect_state or "",
            "fencing_generation": str(step.fencing_generation),
            "status": step.status,
            "error": step.error or "",
            # Raw serialized JSON payload storing preview & structured data
            "_step_json": step.model_dump_json(),
        }

        entry_id = self.r.xadd(trace_k, step_data, maxlen=max_stream_len)
        return entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)

    def record_node_step(
        self,
        run_id: str,
        agent_node: str,
        step_kind: StepKind,
        input_data: Any,
        output_data: Any,
        generation: int = 0,
        status: StepStatus = "ok",
        error: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_args: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        reported_usage: Optional[ReportedUsage] = None,
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        effect_id: Optional[str] = None,
        effect_state: Optional[str] = None,
    ) -> TraceStep:
        """Helper to create and record a node or decision step."""
        received_input = create_trace_payload(input_data, include_full=True)
        produced_output = create_trace_payload(output_data, include_full=True)

        step = TraceStep(
            run_id=run_id,
            agent_node=agent_node,
            step_kind=step_kind,
            received_input=received_input,
            produced_output=produced_output,
            tool_name=tool_name,
            tool_args=tool_args,
            provider=provider,
            model=model,
            reported_usage=reported_usage,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            effect_id=effect_id,
            effect_state=effect_state,
            fencing_generation=generation,
            status=status,
            error=error,
        )
        self.record_step(run_id, step)
        return step

    def record_effect_step(
        self,
        run_id: str,
        effect_record: EffectRecord,
        latency_ms: float = 0.0,
        cache_hit: bool = False,
        generation: int = 0,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> TraceStep:
        """Translates an external EffectRecord into a verified trace step (PRD FR-5.5)."""
        kind: StepKind = "llm" if effect_record.effect_type == "llm" else "tool"
        status: StepStatus = "ok" if effect_record.state == "succeeded" else (
            "error" if effect_record.state in ("failed", "outcome_unknown") else "partial"
        )

        reported = None
        if effect_record.tokens_used > 0:
            reported = ReportedUsage(
                total_tokens=effect_record.tokens_used,
                prompt_tokens=effect_record.tokens_used // 2,
                completion_tokens=effect_record.tokens_used // 2,
            )

        step = TraceStep(
            run_id=run_id,
            agent_node=effect_record.step,
            step_kind=kind,
            tool_name=effect_record.effect_type,
            tool_args=effect_record.input_payload if kind == "tool" else None,
            provider=provider,
            model=model,
            received_input=create_trace_payload(effect_record.input_payload, include_full=True),
            produced_output=create_trace_payload(effect_record.output_payload, include_full=True),
            token_reservation=effect_record.tokens_reserved,
            reported_usage=reported,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            effect_id=effect_record.effect_key,
            effect_state=effect_record.state,
            fencing_generation=generation,
            status=status,
            error=effect_record.error_message,
        )
        self.record_step(run_id, step)
        return step

    def get_trace(
        self,
        run_id: str,
        include_payloads: bool = False,
    ) -> List[TraceStep]:
        """Fetches ordered trace steps from Redis stream, applying payload filtering."""
        trace_k = key_run_trace(run_id)
        raw_entries = self.r.xrange(trace_k, min="-", max="+")
        steps: List[TraceStep] = []

        for _, entry in raw_entries:
            # Decode byte mapping
            decoded_entry = {
                (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                for k, v in entry.items()
            }

            if "_step_json" in decoded_entry:
                try:
                    step = TraceStep.model_validate_json(decoded_entry["_step_json"])
                except Exception as e:
                    logger.warning("Failed to parse stored _step_json: %s", e)
                    continue
            else:
                # Fallback manual reconstruction
                step = TraceStep(
                    step_id=decoded_entry.get("step_id", ""),
                    run_id=run_id,
                    timestamp=float(decoded_entry.get("timestamp", 0.0)),
                    agent_node=decoded_entry.get("agent_node", "unknown"),
                    step_kind=decoded_entry.get("step_kind", "decision"),
                    status=decoded_entry.get("status", "ok"),
                    effect_id=decoded_entry.get("effect_id") or None,
                )

            # Enforce ?include=payloads behavior (PRD FR-5.5)
            if not include_payloads:
                step.received_input.full = None
                step.produced_output.full = None

            steps.append(step)

        return steps

    def verify_trace_equals_ledger(self, run_id: str) -> Tuple[bool, str]:
        """Acceptance test validator: verifies set of external calls in effect ledger equals trace.

        PRD §13 Acceptance Criteria:
        'trace-equals-ledger test passes'
        """
        journal = EffectJournal(self.r)
        effects = journal.list_run_effects(run_id)
        ledger_effect_keys = {e.effect_key for e in effects}

        trace_steps = self.get_trace(run_id, include_payloads=False)
        trace_effect_keys = {s.effect_id for s in trace_steps if s.effect_id}

        if ledger_effect_keys == trace_effect_keys:
            return True, f"Trace and Effect Ledger reconciled successfully ({len(ledger_effect_keys)} effects)."

        in_ledger_only = ledger_effect_keys - trace_effect_keys
        in_trace_only = trace_effect_keys - ledger_effect_keys
        msg = f"Discrepancy detected: In ledger only={in_ledger_only}, In trace only={in_trace_only}"
        return False, msg
