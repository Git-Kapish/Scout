"""Durable Effect Journal and Idempotency Cache (PRD FR-4.2).

Ensures that:
1. Every external effect (LLM, search, fetch) has a stable `effect_key`.
2. Before dispatch, an intent and budget reservation are persisted in `run:{id}:effects`.
3. After dispatch, the response and usage are persisted before the step completes.
4. If a crash occurs mid-flight, ambiguity handling applies (`retry_at_least_once`).
5. In-process crash points from FR-4.6 are invoked at the exact protocol boundaries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, Literal, Optional, Tuple, TypeVar

import redis
from pydantic import BaseModel, Field

from scout.effects.fault_injection import maybe_crash
from scout.store.redis_keys import key_run_effects

logger = logging.getLogger(__name__)

T = TypeVar("T")

EffectState = Literal["prepared", "in_flight", "succeeded", "failed", "outcome_unknown"]


class EffectRecord(BaseModel):
    effect_key: str
    run_id: str
    step: str
    effect_type: Literal["llm", "search", "fetch"]
    state: EffectState = "prepared"
    attempt_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    input_payload: Dict[str, Any] = Field(default_factory=dict)
    output_payload: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    tokens_reserved: int = 0
    tokens_used: int = 0
    created_at_ts: float = Field(default_factory=time.time)
    completed_at_ts: Optional[float] = None
    ambiguous_outcome: bool = False


def compute_effect_key(
    run_id: str,
    step: str,
    effect_type: str,
    input_data: Dict[str, Any],
) -> str:
    """Computes a deterministic, collision-resistant effect key."""
    canonical_input = json.dumps(input_data, sort_keys=True, default=str)
    raw_hash = hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()[:16]
    return f"{run_id}:{step}:{effect_type}:{raw_hash}"


class EffectJournal:
    """Coordinates durable execution of external effects through Redis."""

    def __init__(self, r: redis.Redis):
        self.r = r

    def execute_durable(
        self,
        run_id: str,
        step: str,
        effect_type: Literal["llm", "search", "fetch"],
        input_data: Dict[str, Any],
        dispatch_fn: Callable[[], T],
        serialize_fn: Callable[[T], Dict[str, Any]],
        deserialize_fn: Callable[[Dict[str, Any]], T],
        tokens_reservation: int = 0,
        ambiguity_policy: Literal["retry_at_least_once", "stop_ambiguous"] = "retry_at_least_once",
    ) -> Tuple[T, bool]:
        """Executes a remote effect with two-phase intent journal and crash recovery.

        Returns:
            (result, was_cached)
        """
        effects_key = key_run_effects(run_id)
        eff_key = compute_effect_key(run_id, step, effect_type, input_data)

        # Crash hook 1: before_intent
        maybe_crash("before_intent")

        # Check existing effect in Redis
        raw_existing = self.r.hget(effects_key, eff_key)
        ambiguous = False

        if raw_existing:
            existing = EffectRecord.model_validate_json(raw_existing)
            if existing.state == "succeeded" and existing.output_payload is not None:
                logger.info("Effect cache HIT for %s (attempt %s)", eff_key, existing.attempt_id)
                return deserialize_fn(existing.output_payload), True

            elif existing.state == "in_flight":
                # Worker previously died while request was in-flight
                logger.warning(
                    "Detected IN_FLIGHT effect on re-entry: %s (attempt %s). Applying policy: %s",
                    eff_key,
                    existing.attempt_id,
                    ambiguity_policy,
                )
                if ambiguity_policy == "retry_at_least_once":
                    ambiguous = True
                else:
                    existing.state = "outcome_unknown"
                    self.r.hset(effects_key, eff_key, existing.model_dump_json())
                    raise RuntimeError(f"Ambiguous outcome for mutating effect: {eff_key}")

        # Phase 1: Persist Intent & Budget Reservation
        record = EffectRecord(
            effect_key=eff_key,
            run_id=run_id,
            step=step,
            effect_type=effect_type,
            state="in_flight",
            attempt_id=uuid.uuid4().hex[:8],
            input_payload=input_data,
            tokens_reserved=tokens_reservation,
            ambiguous_outcome=ambiguous,
        )
        self.r.hset(effects_key, eff_key, record.model_dump_json())

        # Crash hook 2: after_intent_before_dispatch
        maybe_crash("after_intent_before_dispatch")

        # Phase 2: Dispatch Remote Call
        try:
            result = dispatch_fn()
        except Exception as e:
            record.state = "failed"
            record.error_message = str(e)
            record.completed_at_ts = time.time()
            self.r.hset(effects_key, eff_key, record.model_dump_json())
            raise

        # Crash hook 3: after_dispatch_before_persist
        maybe_crash("after_dispatch_before_persist")

        # Phase 3: Persist Response & Terminal State
        serialized_output = serialize_fn(result)
        record.state = "succeeded"
        record.output_payload = serialized_output
        record.completed_at_ts = time.time()
        self.r.hset(effects_key, eff_key, record.model_dump_json())

        # Crash hook 4: after_persist_before_checkpoint
        maybe_crash("after_persist_before_checkpoint")

        return result, False

    def get_effect_record(self, run_id: str, effect_key: str) -> Optional[EffectRecord]:
        raw = self.r.hget(key_run_effects(run_id), effect_key)
        if raw:
            return EffectRecord.model_validate_json(raw)
        return None

    def list_run_effects(self, run_id: str) -> list[EffectRecord]:
        raw_dict = self.r.hgetall(key_run_effects(run_id))
        return [EffectRecord.model_validate_json(v) for v in raw_dict.values()]
