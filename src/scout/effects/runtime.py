"""Per-dispatch budget metering and run hooks (PRD FR-2.2, FR-4.2, FR-4.3, FR-4.4).

Nodes open a DispatchMeter around agent calls. Every LLM, search and fetch call goes
through `external_call` (or `DispatchMeter.reserve_llm` for LLM calls), so hard caps,
wall clock and cancellation are checked before each dispatch rather than once per node.
The worker installs RunHooks so the same calls are journaled and counted in Redis;
without a worker the hooks are no-ops and agents run standalone.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, Optional

from scout.config import get_settings
from scout.effects.recorder import EffectRecorder, ReplayMissError, compute_effect_key
from scout.schemas import BudgetLedger


class BudgetExhaustedError(Exception):
    """Raised before dispatch when a hard run limit would be exceeded. args[0] is the reason."""


class RunCancelledError(Exception):
    """Raised before dispatch once cancellation has been requested."""


# Errors that must stop the current step instead of being swallowed as a tool failure.
RUN_STOP_ERRORS = (BudgetExhaustedError, RunCancelledError, ReplayMissError)


def _direct_effect(step, effect_type, inputs, dispatch_fn, serialize_fn, deserialize_fn):
    return dispatch_fn()


@dataclass
class RunHooks:
    is_cancelled: Callable[[], bool] = lambda: False
    record_provider_usage: Callable[[str, int, int], None] = lambda provider, requests, tokens: None
    # (step, effect_type, inputs, dispatch_fn, serialize_fn, deserialize_fn) -> result
    run_effect: Callable[..., Any] = _direct_effect


@dataclass
class DispatchMeter:
    max_llm_calls: int
    max_tokens: int
    deadline_ts: float = 0.0
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    searches: int = 0
    fetches: int = 0
    estimated_tokens: int = 0

    @classmethod
    def from_ledger(cls, ledger: BudgetLedger, max_llm_calls: int, max_tokens: int, deadline_ts: float) -> "DispatchMeter":
        total = ledger.tokens_in + ledger.tokens_out
        return cls(
            max_llm_calls=max_llm_calls,
            max_tokens=max_tokens,
            deadline_ts=deadline_ts,
            llm_calls=ledger.llm_calls,
            tokens_in=ledger.tokens_in,
            tokens_out=ledger.tokens_out,
            searches=ledger.searches,
            fetches=ledger.fetches,
            estimated_tokens=round(ledger.estimated_token_fraction * total),
        )

    def check(self) -> None:
        if current_hooks().is_cancelled():
            raise RunCancelledError("cancelled")
        if self.deadline_ts > 0 and time.time() >= self.deadline_ts:
            raise BudgetExhaustedError("wall_clock_timeout")

    def reserve_llm(self, reservation: int) -> None:
        """Refuses dispatch if the call or token reservation would breach a cap (PRD FR-2.2)."""
        self.check()
        if self.llm_calls >= self.max_llm_calls:
            raise BudgetExhaustedError("llm_calls_exhausted")
        if self.tokens_in + self.tokens_out + reservation > self.max_tokens:
            raise BudgetExhaustedError("token_budget_exhausted")
        # Counted at dispatch: failed and retried requests still count (PRD §5.1, FR-1.1).
        self.llm_calls += 1

    def record_usage(self, prompt_tokens: int, completion_tokens: int, is_estimated: bool) -> None:
        self.tokens_in += prompt_tokens
        self.tokens_out += completion_tokens
        if is_estimated:
            self.estimated_tokens += prompt_tokens + completion_tokens

    def apply(self, ledger: BudgetLedger) -> BudgetLedger:
        total = self.tokens_in + self.tokens_out
        return ledger.model_copy(
            update={
                "llm_calls": self.llm_calls,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "searches": self.searches,
                "fetches": self.fetches,
                "estimated_token_fraction": self.estimated_tokens / total if total else 0.0,
            }
        )


_meter: ContextVar[Optional[DispatchMeter]] = ContextVar("scout_dispatch_meter", default=None)
_hooks: ContextVar[RunHooks] = ContextVar("scout_run_hooks", default=RunHooks())
_recorder: Optional[EffectRecorder] = None


def current_meter() -> Optional[DispatchMeter]:
    return _meter.get()


def current_hooks() -> RunHooks:
    return _hooks.get()


@contextmanager
def metered(meter: DispatchMeter) -> Iterator[DispatchMeter]:
    token = _meter.set(meter)
    try:
        yield meter
    finally:
        _meter.reset(token)


@contextmanager
def run_hooks(hooks: RunHooks) -> Iterator[RunHooks]:
    token = _hooks.set(hooks)
    try:
        yield hooks
    finally:
        _hooks.reset(token)


def external_call(
    effect_type: str,
    step: str,
    inputs: Dict[str, Any],
    dispatch_fn: Callable[[], Any],
    serialize_fn: Callable[[Any], Dict[str, Any]],
    deserialize_fn: Callable[[Dict[str, Any]], Any],
) -> Any:
    """Runs one external effect: pre-dispatch checks, durable journal, then record/replay."""
    meter = current_meter()
    if meter:
        meter.check()
        if effect_type == "search":
            meter.searches += 1
        elif effect_type == "fetch":
            meter.fetches += 1
    elif current_hooks().is_cancelled():
        raise RunCancelledError("cancelled")

    inner = dispatch_fn
    if get_settings().scout_mode != "live":
        global _recorder
        _recorder = _recorder or EffectRecorder()
        key = compute_effect_key(effect_type, step, inputs)
        inner = lambda: _recorder.execute_or_replay(key, dispatch_fn, serialize_fn, deserialize_fn)  # noqa: E731

    return current_hooks().run_effect(step, effect_type, inputs, inner, serialize_fn, deserialize_fn)
