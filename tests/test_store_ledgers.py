"""Tests for Atomic Ledgers (PRD FR-4.3)."""

import pytest
import redis

from scout.store.ledgers import LedgerStore


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=2)
    r.flushdb()
    yield r
    r.flushdb()


def test_run_budget_atomic_increments(redis_client):
    store = LedgerStore(redis_client)
    run_id = "test_run_ledger"

    # Initial check
    b0 = store.get_run_budget(run_id)
    assert b0.tokens_in == 0
    assert b0.llm_calls == 0

    # Incremental update 1
    b1 = store.record_run_usage(run_id, tokens_in=100, tokens_out=50, llm_calls=1, searches=2)
    assert b1.tokens_in == 100
    assert b1.tokens_out == 50
    assert b1.llm_calls == 1
    assert b1.searches == 2

    # Incremental update 2
    b2 = store.record_run_usage(run_id, tokens_in=50, tokens_out=25, llm_calls=1, fetches=3, search_credits=0.05)
    assert b2.tokens_in == 150
    assert b2.tokens_out == 75
    assert b2.llm_calls == 2
    assert b2.searches == 2
    assert b2.fetches == 3
    assert b2.search_credits == pytest.approx(0.05)


def test_provider_daily_usage(redis_client):
    store = LedgerStore(redis_client)
    provider = "openrouter"

    reqs, tokens = store.record_provider_usage(provider, requests=1, tokens=500, utc_date="2026-10-19")
    assert reqs == 1
    assert tokens == 500

    reqs2, tokens2 = store.record_provider_usage(provider, requests=2, tokens=1000, utc_date="2026-10-19")
    assert reqs2 == 3
    assert tokens2 == 1500

    r_get, t_get = store.get_provider_daily_usage(provider, utc_date="2026-10-19")
    assert r_get == 3
    assert t_get == 1500
