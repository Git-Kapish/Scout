"""Tests for Durable Effect Journal (PRD FR-4.2)."""

import pytest
import redis

from scout.effects.journal import EffectJournal, compute_effect_key


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=3)
    r.flushdb()
    yield r
    r.flushdb()


def test_effect_journal_caching_and_states(redis_client):
    journal = EffectJournal(redis_client)
    run_id = "run_journal_test"
    step = "plan"
    effect_type = "llm"
    input_data = {"prompt": "What is quantum computing?", "model": "fake-model"}

    call_count = 0

    def mock_dispatch():
        nonlocal call_count
        call_count += 1
        return {"response": "Quantum computing uses qubits."}

    # First execution: remote call is dispatched
    res1, was_cached1 = journal.execute_durable(
        run_id=run_id,
        step=step,
        effect_type=effect_type,
        input_data=input_data,
        dispatch_fn=mock_dispatch,
        serialize_fn=lambda x: x,
        deserialize_fn=lambda x: x,
        tokens_reservation=100,
    )

    assert was_cached1 is False
    assert res1 == {"response": "Quantum computing uses qubits."}
    assert call_count == 1

    # Verify effect record in Redis is succeeded
    eff_key = compute_effect_key(run_id, step, effect_type, input_data)
    rec = journal.get_effect_record(run_id, eff_key)
    assert rec is not None
    assert rec.state == "succeeded"
    assert rec.output_payload == {"response": "Quantum computing uses qubits."}
    assert rec.tokens_reserved == 100

    # Second execution: re-entry with identical input uses cached output (idempotent)
    res2, was_cached2 = journal.execute_durable(
        run_id=run_id,
        step=step,
        effect_type=effect_type,
        input_data=input_data,
        dispatch_fn=mock_dispatch,
        serialize_fn=lambda x: x,
        deserialize_fn=lambda x: x,
    )

    assert was_cached2 is True
    assert res2 == {"response": "Quantum computing uses qubits."}
    assert call_count == 1  # No extra remote dispatch


def test_effect_journal_in_flight_ambiguity_retry(redis_client):
    journal = EffectJournal(redis_client)
    run_id = "run_ambiguity_test"
    step = "research"
    effect_type = "search"
    input_data = {"query": "quantum algorithms"}

    # Simulate an effect left in 'in_flight' state after a crash
    eff_key = compute_effect_key(run_id, step, effect_type, input_data)
    from scout.effects.journal import EffectRecord
    stale_record = EffectRecord(
        effect_key=eff_key,
        run_id=run_id,
        step=step,
        effect_type=effect_type,
        state="in_flight",
        attempt_id="stale_attempt_1",
        input_payload=input_data,
    )
    redis_client.hset(f"run:{run_id}:effects", eff_key, stale_record.model_dump_json())

    dispatched = False

    def mock_search():
        nonlocal dispatched
        dispatched = True
        return ["hit1", "hit2"]

    # Re-executing with retry_at_least_once policy
    res, was_cached = journal.execute_durable(
        run_id=run_id,
        step=step,
        effect_type=effect_type,
        input_data=input_data,
        dispatch_fn=mock_search,
        serialize_fn=lambda x: {"hits": x},
        deserialize_fn=lambda x: x["hits"],
        ambiguity_policy="retry_at_least_once",
    )

    assert dispatched is True
    assert was_cached is False
    assert res == ["hit1", "hit2"]

    # The record should now be succeeded with ambiguous_outcome == True
    rec = journal.get_effect_record(run_id, eff_key)
    assert rec.state == "succeeded"
    assert rec.ambiguous_outcome is True
