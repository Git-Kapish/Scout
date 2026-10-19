"""Integration tests for Scout Research FastAPI Service (PRD §8, FR-5.1 - FR-5.3)."""

from __future__ import annotations

import json
import time
import uuid
from unittest.mock import MagicMock

import pytest
import redis
from fastapi.testclient import TestClient

from scout.agents.planner import PlannerAgent, PlannerOutputSchema
from scout.agents.researcher import (
    CandidateFinding,
    ExtractionOutput,
    QueryGenerationOutput,
    ResearcherAgent,
)
from scout.agents.writer import WriterAgent
from scout.api.app import app
from scout.fetch.fetcher import PageFetchResult
from scout.graph.supervisor import SupervisorReviewer
from scout.llm import LLMResponse, LLMUsage
from scout.schemas import Claim, Report, ReportSection, SubQuestion
from scout.search import FixtureSearchAdapter, SearchChain, extract_domain
from scout.store.ledgers import LedgerStore
from scout.store.redis_keys import (
    key_idempotency,
    key_queue_runs,
    key_run_budget,
    key_run_events,
    key_run_findings,
    key_run_meta,
    key_run_report,
)
from scout.worker import ResearchWorker


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    return r


@pytest.fixture
def client():
    return TestClient(app)


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "redis" in data
    assert "providers" in data


def test_post_research_success(client, redis_client):
    question = "What are the latest breakthroughs in agentic AI?"
    resp = client.post("/research", json={"question": question})

    assert resp.status_code == 202
    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "queued"
    assert data["links"]["status"] == f"/research/{data['run_id']}"
    assert data["links"]["events"] == f"/research/{data['run_id']}/events"
    assert data["links"]["trace"] == f"/research/{data['run_id']}/trace"

    run_id = data["run_id"]

    # Verify Redis state
    meta = redis_client.hgetall(key_run_meta(run_id))
    assert meta[b"status"].decode() == "queued"
    assert meta[b"question"].decode() == question

    # Verify queued in queue:runs
    queued_runs = redis_client.lrange(key_queue_runs(), 0, -1)
    assert run_id.encode() in queued_runs

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)
    redis_client.lrem(key_queue_runs(), 0, run_id)


def test_post_research_idempotency(client, redis_client):
    idem_key = f"idem_{uuid.uuid4().hex[:8]}"
    question = "Idempotent question test"

    # First submission
    resp1 = client.post(
        "/research",
        json={"question": question},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp1.status_code == 202
    run_id1 = resp1.json()["run_id"]

    # Second submission with identical Idempotency-Key
    resp2 = client.post(
        "/research",
        json={"question": question},
        headers={"Idempotency-Key": idem_key},
    )
    assert resp2.status_code == 202
    run_id2 = resp2.json()["run_id"]

    # Must return exact same run_id without duplicate queuing
    assert run_id1 == run_id2

    # Verify queue contains run_id only once
    queued_runs = redis_client.lrange(key_queue_runs(), 0, -1)
    assert queued_runs.count(run_id1.encode()) == 1

    # Cleanup
    redis_client.delete(key_idempotency(idem_key))
    for k in redis_client.keys(f"*{run_id1}*"):
        redis_client.delete(k)
    redis_client.lrem(key_queue_runs(), 0, run_id1)


def test_admission_control_rejection(client, redis_client, monkeypatch):
    from scout.config import get_settings
    settings = get_settings()
    provider = settings.get_role_primary("planner").provider

    # Artificially record high provider daily usage
    store = LedgerStore(redis_client)
    store.record_provider_usage(provider, requests=100)

    try:
        resp = client.post("/research", json={"question": "Will this exceed quota?"})
        assert resp.status_code == 429
        data = resp.json()
        assert data["code"] == "daily_allowance"
        assert data["remaining_calls"] == 0
        assert data["retry_after_s"] > 0
        assert "Retry-After" in resp.headers
    finally:
        # Reset provider counter for today
        from scout.store.redis_keys import key_provider_daily_requests
        redis_client.delete(key_provider_daily_requests(provider))


def test_get_research_status_not_found(client):
    resp = client.get("/research/nonexistent_run_999")
    assert resp.status_code == 404
    data = resp.json()
    assert data["detail"]["code"] == "not_found"


def test_get_research_status_completed(client, redis_client):
    run_id = f"test_run_{uuid.uuid4().hex[:8]}"

    # Populate sample Redis data
    now = time.time()
    redis_client.hset(
        key_run_meta(run_id),
        mapping={
            "run_id": run_id,
            "status": "completed",
            "question": "Sample Question?",
            "created_at_ts": str(now - 12.5),
            "completed_at_ts": str(now),
            "subquestions_total": "3",
            "subquestions_done": "3",
        },
    )

    redis_client.hset(
        key_run_budget(run_id),
        mapping={
            "tokens_in": "1200",
            "tokens_out": "450",
            "llm_calls": "5",
            "searches": "4",
        },
    )

    sample_report = {
        "title": "Sample Report",
        "sections": [
            {
                "title": "Section 1",
                "content": "Content with facts.",
                "claims": [{"text": "Claim Alpha", "finding_ids": ["find_001"]}],
            }
        ],
        "coverage_gaps": [],
        "markdown": "# Sample Report\nClaim Alpha [find_001].",
    }
    redis_client.set(key_run_report(run_id), json.dumps(sample_report))

    sample_finding = {
        "finding_id": "find_001",
        "subquestion_id": "sq_1",
        "claim": "Claim Alpha",
        "snippet": "verbatim text snippet",
        "source_url": "https://example.com/alpha",
        "title": "Example Alpha",
        "retrieved_at": "2026-10-19T12:00:00Z",
        "verified": True,
    }
    redis_client.hset(key_run_findings(run_id), "find_001", json.dumps(sample_finding))

    resp = client.get(f"/research/{run_id}")
    assert resp.status_code == 200
    data = resp.json()

    assert data["run_id"] == run_id
    assert data["status"] == "completed"
    assert data["question"] == "Sample Question?"
    assert data["progress"]["subquestions_total"] == 3
    assert data["progress"]["subquestions_done"] == 3
    assert data["budget"]["tokens_used"] == 1650
    assert data["budget"]["llm_calls"] == 5
    assert data["budget"]["searches"] == 4
    assert data["budget"]["elapsed_s"] >= 12.0

    assert data["report"] is not None
    assert len(data["report"]["claims"]) == 1
    assert data["report"]["claims"][0]["citations"] == ["find_001"]
    assert len(data["report"]["sources"]) == 1
    assert data["report"]["sources"][0]["url"] == "https://example.com/alpha"

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)


def test_cancel_research_run(client, redis_client):
    run_id = f"test_cancel_{uuid.uuid4().hex[:8]}"
    redis_client.hset(
        key_run_meta(run_id),
        mapping={"run_id": run_id, "status": "running", "question": "Cancel test"},
    )

    resp = client.post(f"/research/{run_id}/cancel")
    assert resp.status_code == 202
    assert resp.json()["status"] == "cancel_requested"

    meta = redis_client.hgetall(key_run_meta(run_id))
    assert meta[b"cancellation_requested"].decode() == "1"

    # Check event stream
    events = redis_client.xrange(key_run_events(run_id), "-", "+")
    assert any(e[1].get(b"event") == b"run_cancel_requested" for e in events)

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)


def test_resume_research_run(client, redis_client):
    run_id = f"test_resume_{uuid.uuid4().hex[:8]}"
    redis_client.hset(
        key_run_meta(run_id),
        mapping={"run_id": run_id, "status": "failed", "question": "Resume test"},
    )

    resp = client.post(f"/research/{run_id}/resume")
    assert resp.status_code == 202
    assert resp.json()["status"] == "resumed"

    meta = redis_client.hgetall(key_run_meta(run_id))
    assert meta[b"status"].decode() == "queued"

    # Verify re-enqueued
    queued = redis_client.lrange(key_queue_runs(), 0, -1)
    assert run_id.encode() in queued

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)
    redis_client.lrem(key_queue_runs(), 0, run_id)


def test_stream_events_and_replay(client, redis_client):
    run_id = f"test_stream_{uuid.uuid4().hex[:8]}"
    redis_client.hset(
        key_run_meta(run_id),
        mapping={"run_id": run_id, "status": "completed", "question": "Stream test"},
    )

    events_k = key_run_events(run_id)
    id1 = redis_client.xadd(events_k, {"event": "run_started", "data": "1"})
    redis_client.xadd(events_k, {"event": "plan_created", "data": "2"})
    redis_client.xadd(events_k, {"event": "run_completed", "data": "3"})

    # 1. Full stream request
    with client.stream("GET", f"/research/{run_id}/events") as resp:
        assert resp.status_code == 200
        lines = list(resp.iter_lines())
        text = "\n".join(lines)
        assert "event: run_started" in text
        assert "event: plan_created" in text
        assert "event: run_completed" in text

    # 2. Replay with Last-Event-ID = id1
    with client.stream("GET", f"/research/{run_id}/events", headers={"Last-Event-ID": id1.decode()}) as resp:
        assert resp.status_code == 200
        lines = list(resp.iter_lines())
        text = "\n".join(lines)
        assert "event: run_started" not in text
        assert "event: plan_created" in text
        assert "event: run_completed" in text

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)


def build_test_harness():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    pages = {
        "https://test.org/p1": "Page 1 contains key fact alpha verified verbatim for question.",
        "https://test2.org/p2": "Page 2 contains key fact beta verified verbatim for question.",
    }

    mock_llm = MagicMock()
    mock_llm.estimate_prompt_tokens.return_value = 50
    mock_llm.generate.return_value = LLMResponse(
        content="Plan",
        parsed=PlannerOutputSchema(sub_questions=subqs),
        provider="fake",
        model="fake",
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        latency_s=0.01,
    )
    planner = PlannerAgent(llm_client=mock_llm)

    search_adapter = FixtureSearchAdapter()
    for q in ["query_1", "query_2", "targeted research query"]:
        hits = [
            {"title": f"Doc for {url}", "url": url, "snippet": "snippet", "domain": extract_domain(url)}
            for url in pages.keys()
        ]
        search_adapter.register_fixture(q, hits)
    search_chain = SearchChain(providers=[search_adapter])

    mock_fetcher = MagicMock()
    mock_fetcher.fetch.side_effect = lambda url: PageFetchResult(
        url=url, status="ok", text_content=pages[url], page_hash=f"h_{abs(hash(url))}", status_code=200
    )

    mock_res_llm = MagicMock()
    mock_res_llm.estimate_prompt_tokens.return_value = 50

    candidates = [
        CandidateFinding(claim="Fact alpha", snippet="key fact alpha verified verbatim"),
        CandidateFinding(claim="Fact beta", snippet="key fact beta verified verbatim"),
    ]

    def r_gen(role, messages, response_model=None, **kwargs):
        if response_model == QueryGenerationOutput:
            return LLMResponse(
                content="",
                parsed=QueryGenerationOutput(queries=["targeted research query"]),
                provider="fake",
                model="fake",
                usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
                latency_s=0.01,
            )
        elif response_model == ExtractionOutput:
            return LLMResponse(
                content="",
                parsed=ExtractionOutput(findings=candidates),
                provider="fake",
                model="fake",
                usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
                latency_s=0.01,
            )
        return LLMResponse(content="", parsed=None, provider="fake", model="fake", usage=LLMUsage(), latency_s=0.01)

    mock_res_llm.generate.side_effect = r_gen
    researcher = ResearcherAgent(llm_client=mock_res_llm, search_chain=search_chain, fetcher=mock_fetcher)
    supervisor = SupervisorReviewer()

    writer = WriterAgent(llm_client=mock_llm)

    def valid_write(question, plan, findings, **kwargs):
        fids = [f.finding_id for f in findings]
        return Report(
            title="E2E API Report",
            summary="Summary",
            sections=[ReportSection(title="S1", content="C", claims=[Claim(text="Claim A", finding_ids=fids[:2])])],
            coverage_gaps=[],
            markdown="# E2E API Report\nClaim A verified.",
        )

    writer.write = MagicMock(side_effect=valid_write)
    return planner, researcher, supervisor, writer


def test_api_to_worker_end_to_end_execution(client, redis_client):
    """End-to-end flow: Client posts research run -> Worker processes run from queue -> Client queries completed state."""
    question = "End-to-end integration test question"
    resp = client.post("/research", json={"question": question})
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    planner, researcher, supervisor, writer = build_test_harness()
    worker = ResearchWorker(redis_url="redis://127.0.0.1:6379/0", worker_id="test_api_worker")

    # Worker picks up from queue
    state = worker.process_next_run(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    assert state is not None
    assert state.run_id == run_id
    assert state.status in ("completed", "completed_partial")

    # Client queries status
    status_resp = client.get(f"/research/{run_id}")
    assert status_resp.status_code == 200
    status_data = status_resp.json()

    assert status_data["status"] == state.status
    assert status_data["report"] is not None
    assert "E2E API Report" in status_data["report"]["markdown"]

    # Cleanup
    for k in redis_client.keys(f"*{run_id}*"):
        redis_client.delete(k)
