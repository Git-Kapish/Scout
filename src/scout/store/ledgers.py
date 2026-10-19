"""Atomic Ledgers for Per-Run Budgets and Daily Provider Quotas (PRD FR-4.3).

Provides:
- Atomic increments for per-run budget ledger via HINCRBY.
- Atomic increments for daily provider request/token counters.
- Reading and deserialization of ledgers.
"""

from __future__ import annotations

from typing import Optional

import redis

from scout.schemas import BudgetLedger
from scout.store.redis_keys import (
    key_provider_daily_requests,
    key_provider_daily_tokens,
    key_run_budget,
)


class LedgerStore:
    def __init__(self, r: redis.Redis):
        self.r = r

    def record_run_usage(
        self,
        run_id: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        llm_calls: int = 0,
        searches: int = 0,
        fetches: int = 0,
        search_credits: float = 0.0,
    ) -> BudgetLedger:
        """Atomically increments the run budget ledger and returns the current total."""
        b_key = key_run_budget(run_id)
        pipe = self.r.pipeline()
        if tokens_in > 0:
            pipe.hincrby(b_key, "tokens_in", tokens_in)
        if tokens_out > 0:
            pipe.hincrby(b_key, "tokens_out", tokens_out)
        if llm_calls > 0:
            pipe.hincrby(b_key, "llm_calls", llm_calls)
        if searches > 0:
            pipe.hincrby(b_key, "searches", searches)
        if fetches > 0:
            pipe.hincrby(b_key, "fetches", fetches)
        if search_credits > 0:
            pipe.hincrbyfloat(b_key, "search_credits", search_credits)

        pipe.hgetall(b_key)
        results = pipe.execute()
        raw_hash = results[-1] if results else {}

        return self._parse_ledger_hash(raw_hash)

    def get_run_budget(self, run_id: str) -> BudgetLedger:
        """Retrieves the current budget ledger for a run."""
        b_key = key_run_budget(run_id)
        raw = self.r.hgetall(b_key)
        return self._parse_ledger_hash(raw)

    def record_provider_usage(
        self,
        provider: str,
        requests: int = 1,
        tokens: int = 0,
        utc_date: Optional[str] = None,
    ) -> tuple[int, int]:
        """Atomically increments daily provider request and token counters.

        Returns:
            (total_requests_today, total_tokens_today)
        """
        req_key = key_provider_daily_requests(provider, utc_date=utc_date)
        tok_key = key_provider_daily_tokens(provider, utc_date=utc_date)

        pipe = self.r.pipeline()
        pipe.incrby(req_key, requests)
        pipe.incrby(tok_key, tokens)
        res = pipe.execute()
        return int(res[0]), int(res[1])

    def get_provider_daily_usage(
        self,
        provider: str,
        utc_date: Optional[str] = None,
    ) -> tuple[int, int]:
        """Reads daily provider request and token usage."""
        req_key = key_provider_daily_requests(provider, utc_date=utc_date)
        tok_key = key_provider_daily_tokens(provider, utc_date=utc_date)

        req_val = self.r.get(req_key) or 0
        tok_val = self.r.get(tok_key) or 0
        return int(req_val), int(tok_val)

    def _parse_ledger_hash(self, raw: dict) -> BudgetLedger:
        parsed = {}
        for k, v in raw.items():
            key_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            val_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
            if key_str in ("search_credits", "credits_used"):
                parsed["search_credits"] = float(val_str)
            else:
                try:
                    parsed[key_str] = int(val_str)
                except ValueError:
                    parsed[key_str] = val_str
        return BudgetLedger(**parsed)
