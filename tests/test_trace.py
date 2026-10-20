"""Tests for Ordered Step Trace, Redaction, and Effect Ledger Reconciliation (PRD FR-5.5, §8, §13).

Covers:
1. Ordered step log recording in Redis stream.
2. API endpoint GET /research/{id}/trace.
3. Payload redaction (API keys, Bearer tokens, URL query params, Authorization headers).
4. Payload inlining behavior: default preview only vs ?include=payloads.
5. Acceptance Criteria: trace-equals-ledger reconciliation test passes.
"""

from __future__ import annotations

import json
import uuid
import pytest
import redis
from fastapi.testclient import TestClient

from scout.api.app import app
from scout.effects.journal import EffectJournal, EffectRecord
from scout.store.redis_keys import key_run_meta, key_run_trace
from scout.trace import (
    ReportedUsage,
    ResearchTraceResponse,
    StepKind,
    TracePayload,
    TraceStep,
    TraceStore,
    create_trace_payload,
    redact_payload,
    sanitize_url,
)


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    return r


@pytest.fixture
def client():
    return TestClient(app)


def test_sanitize_url_removes_sensitive_query_parameters():
    url = "https://api.tavily.com/search?query=agentic+ai&api_key=tvly-secret123&token=tok456&format=json"
    clean = sanitize_url(url)
    assert "tvly-secret123" not in clean
    assert "tok456" not in clean
    assert "api_key=%5BREDACTED%5D" in clean or "api_key=[REDACTED]" in clean
    assert "query=agentic+ai" in clean
    assert "format=json" in clean


def test_redact_payload_recursive():
    payload = {
        "user_query": "research on AI",
        "api_key": "sk-1234567890abcdef1234567890",
        "nested": {
            "Authorization": "Bearer secret_jwt_token_987654321",
            "url": "https://service.com/api?token=secret_param_token",
        },
        "items": [
            "Normal item",
            "Sensitive: Bearer my_super_secret_token_abc123",
        ],
    }

    redacted = redact_payload(payload)
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["Authorization"] == "[REDACTED]"
    assert "secret_param_token" not in redacted["nested"]["url"]
    assert "[REDACTED]" in redacted["nested"]["url"]
    assert "my_super_secret_token" not in redacted["items"][1]


def test_create_trace_payload_preview_and_hash():
    # Small payload
    small = {"message": "hello world"}
    p_small = create_trace_payload(small, include_full=False)
    assert p_small.reference is None
    assert "hello world" in p_small.preview
    assert p_small.full is None

    # Large payload (> 2 KB)
    large_text = "evidence " * 500  # ~4.5 KB
    large_dict = {"text": large_text}
    p_large = create_trace_payload(large_dict, include_full=True)
    assert p_large.reference is not None
    assert p_large.reference.startswith("hash:")
    assert len(p_large.preview) <= 2100
    assert "... [truncated" in p_large.preview
    assert p_large.full is not None


def test_trace_store_record_and_get(redis_client):
    run_id = f"trace_test_{uuid.uuid4().hex[:8]}"
    store = TraceStore(redis_client)

    step1 = store.record_node_step(
        run_id=run_id,
        agent_node="planner",
        step_kind="llm",
        input_data={"question": "What is AI?"},
        output_data={"sub_questions": ["sq_1"]},
        provider="openrouter",
        model="test-model",
    )

    step2 = store.record_node_step(
        run_id=run_id,
        agent_node="researcher",
        step_kind="tool",
        input_data={"subquestion_id": "sq_1"},
        output_data={"findings": ["f_1"]},
        tool_name="search",
        tool_args={"query": "AI research", "api_key": "sk-secret"},
    )

    # 1. Fetch trace without full payloads
    steps_preview = store.get_trace(run_id, include_payloads=False)
    assert len(steps_preview) == 2
    assert steps_preview[0].agent_node == "planner"
    assert steps_preview[0].step_kind == "llm"
    assert steps_preview[0].received_input.preview != ""
    assert steps_preview[0].received_input.full is None

    assert steps_preview[1].agent_node == "researcher"
    assert steps_preview[1].tool_args["api_key"] == "[REDACTED]"

    # 2. Fetch trace with inlined payloads
    steps_full = store.get_trace(run_id, include_payloads=True)
    assert len(steps_full) == 2
    assert steps_full[0].received_input.full is not None
    assert steps_full[0].received_input.full["question"] == "What is AI?"

    # Cleanup
    redis_client.delete(key_run_trace(run_id))


def test_get_research_trace_api(client, redis_client):
    run_id = f"run_trace_api_{uuid.uuid4().hex[:8]}"
    redis_client.hset(key_run_meta(run_id), "status", "completed")
    redis_client.hset(key_run_meta(run_id), "question", "Trace API test")

    store = TraceStore(redis_client)
    store.record_node_step(
        run_id=run_id,
        agent_node="writer",
        step_kind="llm",
        input_data={"prompt": "Write report with sk-secret_api_key_12345"},
        output_data={"markdown": "# Title"},
        latency_ms=120.5,
    )

    # 1. Test GET /research/{run_id}/trace default
    resp = client.get(f"/research/{run_id}/trace")
    assert resp.status_code == 200
    data = resp.json()
    assert data["run_id"] == run_id
    assert data["total_steps"] == 1
    step = data["steps"][0]
    assert step["agent_node"] == "writer"
    assert step["step_kind"] == "llm"
    assert step["latency_ms"] == pytest.approx(120.5)
    assert "sk-secret" not in step["received_input"]["preview"]
    assert step["received_input"]["full"] is None

    # 2. Test GET /research/{run_id}/trace?include=payloads
    resp_full = client.get(f"/research/{run_id}/trace?include=payloads")
    assert resp_full.status_code == 200
    data_full = resp_full.json()
    step_full = data_full["steps"][0]
    assert step_full["received_input"]["full"] is not None
    assert "sk-secret" not in str(step_full["received_input"]["full"])

    # 3. Test 404 for non-existent run
    resp_404 = client.get("/research/non_existent_run_id/trace")
    assert resp_404.status_code == 404

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)


def test_trace_equals_ledger_acceptance_criteria(redis_client):
    """PRD Acceptance Criteria: 'trace-equals-ledger test passes'.

    Verifies that the set of external calls recorded in the effect ledger
    equals the set of external calls recorded in the trace.
    """
    run_id = f"trace_ledger_{uuid.uuid4().hex[:8]}"
    journal = EffectJournal(redis_client)
    store = TraceStore(redis_client)

    # Clean Redis
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)

    # 1. Execute external effect 1 (LLM)
    res1, cached1 = journal.execute_durable(
        run_id=run_id,
        step="planner",
        effect_type="llm",
        input_data={"messages": [{"role": "user", "content": "plan queries"}]},
        dispatch_fn=lambda: {"plan": "q1, q2"},
        serialize_fn=lambda x: x,
        deserialize_fn=lambda x: x,
        tokens_reservation=100,
    )
    assert cached1 is False

    # 2. Execute external effect 2 (Search tool)
    res2, cached2 = journal.execute_durable(
        run_id=run_id,
        step="researcher_sq1",
        effect_type="search",
        input_data={"query": "latest AI news"},
        dispatch_fn=lambda: [{"url": "https://example.com/1", "title": "AI"}],
        serialize_fn=lambda x: {"hits": x},
        deserialize_fn=lambda x: x["hits"],
    )
    assert cached2 is False

    # 3. Execute external effect 3 (Fetch tool)
    res3, cached3 = journal.execute_durable(
        run_id=run_id,
        step="researcher_sq1",
        effect_type="fetch",
        input_data={"url": "https://example.com/1"},
        dispatch_fn=lambda: {"text": "Page text content"},
        serialize_fn=lambda x: x,
        deserialize_fn=lambda x: x,
    )
    assert cached3 is False

    # Also record internal graph decision steps in the trace (which are not external effects)
    store.record_node_step(
        run_id=run_id,
        agent_node="init_run",
        step_kind="decision",
        input_data={},
        output_data={"status": "running"},
    )
    store.record_node_step(
        run_id=run_id,
        agent_node="review",
        step_kind="decision",
        input_data={},
        output_data={"decision": "accept"},
    )

    # Run the acceptance test reconciliation
    reconciled, msg = store.verify_trace_equals_ledger(run_id)
    assert reconciled is True, f"Reconciliation failed: {msg}"
    assert "reconciled successfully (3 effects)" in msg

    # Intentionally inject an artificial discrepancy to verify detection
    bogus_eff = EffectRecord(
        effect_key=f"{run_id}:unreconciled_call",
        run_id=run_id,
        step="orphan_step",
        effect_type="llm",
        state="succeeded",
    )
    redis_client.hset(
        f"run:{run_id}:effects",
        bogus_eff.effect_key,
        bogus_eff.model_dump_json(),
    )

    reconciled_bad, msg_bad = store.verify_trace_equals_ledger(run_id)
    assert reconciled_bad is False
    assert "Discrepancy detected" in msg_bad
    assert bogus_eff.effect_key in msg_bad

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)
