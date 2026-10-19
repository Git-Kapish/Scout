"""Tests for Redis Lua scripts: Fencing, Atomic Commit Pointer, and Leases (PRD FR-4.1, FR-4.4)."""

import json

import pytest
import redis

from scout.store.lua_scripts import ScriptRegistry
from scout.store.redis_keys import (
    key_lease,
    key_run_committed,
    key_run_generation,
    key_runs_active,
)


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=1)
    r.flushdb()
    yield r
    r.flushdb()


def test_advance_commit_fencing(redis_client):
    registry = ScriptRegistry(redis_client)
    run_id = "test_run_commit"
    gen_key = key_run_generation(run_id)
    commit_key = key_run_committed(run_id)

    # Setup initial generation = 1
    redis_client.set(gen_key, 1)

    # 1. Valid commit at seq 1
    ok, reason = registry.advance_commit(
        gen_key, commit_key, generation=1, seq=1,
        committed_payload={"checkpoint_id": "cp_1", "generation": 1, "seq": 1, "node": "plan"}
    )
    assert ok is True
    assert reason == "ok"
    stored = json.loads(redis_client.get(commit_key))
    assert stored["checkpoint_id"] == "cp_1"
    assert stored["seq"] == 1

    # 2. Valid commit at seq 2
    ok, reason = registry.advance_commit(
        gen_key, commit_key, generation=1, seq=2,
        committed_payload={"checkpoint_id": "cp_2", "generation": 1, "seq": 2, "node": "research"}
    )
    assert ok is True
    stored = json.loads(redis_client.get(commit_key))
    assert stored["checkpoint_id"] == "cp_2"

    # 3. Reject stale sequence (seq 2 <= seq 2)
    ok, reason = registry.advance_commit(
        gen_key, commit_key, generation=1, seq=2,
        committed_payload={"checkpoint_id": "cp_stale", "generation": 1, "seq": 2, "node": "research"}
    )
    assert ok is False
    assert reason == "stale_seq"
    # Ensure committed pointer unchanged
    stored = json.loads(redis_client.get(commit_key))
    assert stored["checkpoint_id"] == "cp_2"

    # 4. Bump generation to 2 (e.g. new worker took over)
    redis_client.set(gen_key, 2)

    # 5. Old worker (generation=1) tries to advance commit pointer -> rejected
    ok, reason = registry.advance_commit(
        gen_key, commit_key, generation=1, seq=3,
        committed_payload={"checkpoint_id": "cp_old_worker", "generation": 1, "seq": 3, "node": "review"}
    )
    assert ok is False
    assert reason == "stale_generation"
    stored = json.loads(redis_client.get(commit_key))
    assert stored["checkpoint_id"] == "cp_2"


def test_fenced_writes(redis_client):
    registry = ScriptRegistry(redis_client)
    run_id = "test_run_fenced_write"
    gen_key = key_run_generation(run_id)
    target_hash = f"run:{run_id}:meta"
    target_str = f"run:{run_id}:report"

    redis_client.set(gen_key, 5)

    # Stale generation writes fail
    ok, reason = registry.fenced_hset(gen_key, target_hash, generation=4, mapping={"status": "completed"})
    assert ok is False
    assert reason == "stale_generation"
    assert redis_client.hget(target_hash, "status") is None

    ok, reason = registry.fenced_set(gen_key, target_str, generation=4, value="Report Content")
    assert ok is False
    assert reason == "stale_generation"
    assert redis_client.get(target_str) is None

    # Matching generation writes succeed
    ok, reason = registry.fenced_hset(gen_key, target_hash, generation=5, mapping={"status": "running", "step": "plan"})
    assert ok is True
    assert redis_client.hget(target_hash, "status").decode() == "running"
    assert redis_client.hget(target_hash, "step").decode() == "plan"

    ok, reason = registry.fenced_set(gen_key, target_str, generation=5, value="Valid Report")
    assert ok is True
    assert redis_client.get(target_str).decode() == "Valid Report"


def test_lease_lifecycle(redis_client):
    registry = ScriptRegistry(redis_client)
    run_id = "test_run_lease"
    lease_k = key_lease(run_id)
    gen_k = key_run_generation(run_id)
    active_k = key_runs_active()

    # 1. Worker 1 acquires lease
    ok, reason, gen1 = registry.acquire_lease(
        lease_k, gen_k, active_k, worker_id="worker_A", ttl_seconds=10, run_id=run_id
    )
    assert ok is True
    assert gen1 == 1
    assert redis_client.sismember(active_k, run_id)

    # 2. Worker 2 tries to acquire without force -> rejected while lease active
    ok, reason, gen2 = registry.acquire_lease(
        lease_k, gen_k, active_k, worker_id="worker_B", ttl_seconds=10, run_id=run_id, force=False
    )
    assert ok is False
    assert reason == "lease_active"

    # 3. Worker 1 renews lease
    ok, reason = registry.renew_lease(lease_k, gen_k, worker_id="worker_A", generation=gen1, ttl_seconds=15)
    assert ok is True

    # 4. Worker 2 steals lease with force=True (or after lease expired)
    ok, reason, gen2 = registry.acquire_lease(
        lease_k, gen_k, active_k, worker_id="worker_B", ttl_seconds=10, run_id=run_id, force=True
    )
    assert ok is True
    assert gen2 == 2

    # 5. Stale Worker 1 attempts to renew -> rejected
    ok, reason = registry.renew_lease(lease_k, gen_k, worker_id="worker_A", generation=gen1, ttl_seconds=10)
    assert ok is False
    assert reason == "stale_generation"

    # 6. Worker 2 releases lease as terminal
    ok, reason = registry.release_lease(
        lease_k, gen_k, active_k, worker_id="worker_B", generation=gen2, run_id=run_id, is_terminal=True
    )
    assert ok is True
    assert redis_client.get(lease_k) is None
    assert not redis_client.sismember(active_k, run_id)
