"""Integration tests for ResearchWorker execution and stalled worker fencing (PRD FR-4.1, FR-4.4)."""

import json
import uuid
from unittest.mock import MagicMock

import pytest
import redis

from scout.agents.planner import PlannerAgent, PlannerOutputSchema
from scout.agents.researcher import (
    CandidateFinding,
    ExtractionOutput,
    QueryGenerationOutput,
    ResearcherAgent,
)
from scout.agents.writer import WriterAgent
from scout.fetch.fetcher import PageFetchResult
from scout.graph.supervisor import SupervisorReviewer
from scout.llm import LLMResponse, LLMUsage
from scout.schemas import Claim, Report, ReportSection, SubQuestion
from scout.search import FixtureSearchAdapter, SearchChain, extract_domain
from scout.store.ledgers import LedgerStore
from scout.store.redis_keys import (
    key_run_committed,
    key_run_findings,
    key_run_meta,
    key_run_report,
)
from scout.trace.store import TraceStore
from scout.worker import ResearchWorker


def make_mock_llm_resp(parsed) -> LLMResponse:
    return LLMResponse(
        content=str(parsed),
        parsed=parsed,
        provider="fake",
        model="fake-model",
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        latency_s=0.01,
    )


def build_mock_harness():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    pages = {
        "https://test.org/page1": "Page 1 contains key fact alpha verified verbatim for question.",
        "https://test2.org/page2": "Page 2 contains key fact beta verified verbatim for question.",
    }

    mock_planner_llm = MagicMock()
    mock_planner_llm.estimate_prompt_tokens.return_value = 50
    mock_planner_llm.generate.return_value = make_mock_llm_resp(PlannerOutputSchema(sub_questions=subqs))
    planner = PlannerAgent(llm_client=mock_planner_llm)

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

    mock_researcher_llm = MagicMock()
    mock_researcher_llm.estimate_prompt_tokens.return_value = 50

    candidates = [
        CandidateFinding(claim="Fact alpha", snippet="key fact alpha verified verbatim"),
        CandidateFinding(claim="Fact beta", snippet="key fact beta verified verbatim"),
    ]

    def researcher_generate(role, messages, response_model=None, **kwargs):
        if response_model == QueryGenerationOutput:
            return make_mock_llm_resp(QueryGenerationOutput(queries=["targeted research query"]))
        elif response_model == ExtractionOutput:
            return make_mock_llm_resp(ExtractionOutput(findings=candidates))
        return make_mock_llm_resp(None)

    mock_researcher_llm.generate.side_effect = researcher_generate
    researcher = ResearcherAgent(llm_client=mock_researcher_llm, search_chain=search_chain, fetcher=mock_fetcher)

    supervisor = SupervisorReviewer()

    mock_writer_llm = MagicMock()
    mock_writer_llm.estimate_prompt_tokens.return_value = 50
    writer = WriterAgent(llm_client=mock_writer_llm)

    def valid_writer(question, plan, findings, **kwargs):
        fids = [f.finding_id for f in findings]
        return Report(
            title="Durable Report",
            summary="Summary",
            sections=[ReportSection(title="S1", content="C", claims=[Claim(text="Claim A", finding_ids=fids[:2])])],
            coverage_gaps=[],
            markdown="# Durable Report",
        )

    writer.write = MagicMock(side_effect=valid_writer)

    return planner, researcher, supervisor, writer


@pytest.fixture
def clean_redis():
    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    # Clear only test run keys
    yield r


def test_worker_end_to_end_execution(clean_redis):
    run_id = f"worker_test_{uuid.uuid4().hex[:8]}"
    for k in clean_redis.keys(f"*{run_id}*"):
        clean_redis.delete(k)
    worker = ResearchWorker(redis_url="redis://127.0.0.1:6379/0", worker_id="worker_1")
    planner, researcher, supervisor, writer = build_mock_harness()

    final_state = worker.execute_run(
        run_id=run_id,
        initial_question="What are A and B?",
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    assert final_state.status == "completed"
    assert final_state.report is not None

    # Verify atomic commit pointer was advanced in Redis
    committed_raw = clean_redis.get(key_run_committed(run_id))
    assert committed_raw is not None
    committed = json.loads(committed_raw)
    assert committed["seq"] > 0
    assert "checkpoint_id" in committed

    # Verify read-model keys in Redis
    meta = clean_redis.hgetall(key_run_meta(run_id))
    assert meta[b"status"].decode() == "completed"

    report_raw = clean_redis.get(key_run_report(run_id))
    assert report_raw is not None
    report_dict = json.loads(report_raw)
    assert report_dict["title"] == "Durable Report"

    findings = clean_redis.hgetall(key_run_findings(run_id))
    assert len(findings) >= 4

    # Search and fetch went through the effect journal, and every effect is in the trace
    assert clean_redis.hlen(f"run:{run_id}:effects") > 0
    ok, msg = TraceStore(clean_redis).verify_trace_equals_ledger(run_id)
    assert ok, msg

    # Ledger counts come from the dispatch meter
    budget = LedgerStore(clean_redis).get_run_budget(run_id)
    assert budget.searches > 0 and budget.fetches > 0


def test_worker_hooks_record_provider_usage_and_read_cancellation(clean_redis):
    run_id = f"hooks_{uuid.uuid4().hex[:8]}"
    provider = f"test_provider_{uuid.uuid4().hex[:6]}"
    worker = ResearchWorker(redis_url="redis://127.0.0.1:6379/0", worker_id="worker_hooks")
    hooks = worker._run_hooks(run_id)

    hooks.record_provider_usage(provider, 1, 0)
    hooks.record_provider_usage(provider, 0, 120)
    assert LedgerStore(clean_redis).get_provider_daily_usage(provider) == (1, 120)

    assert hooks.is_cancelled() is False
    clean_redis.hset(key_run_meta(run_id), "cancellation_requested", "1")
    assert hooks.is_cancelled() is True

    for k in clean_redis.keys(f"*{run_id}*") + clean_redis.keys(f"provider:{provider}:*"):
        clean_redis.delete(k)


def test_stalled_worker_preemption_and_fencing(clean_redis):
    run_id = f"stalled_run_{uuid.uuid4().hex[:8]}"
    for k in clean_redis.keys(f"*{run_id}*"):
        clean_redis.delete(k)
    planner, researcher, supervisor, writer = build_mock_harness()

    worker_a = ResearchWorker(redis_url="redis://127.0.0.1:6379/0", worker_id="worker_A")
    worker_b = ResearchWorker(redis_url="redis://127.0.0.1:6379/0", worker_id="worker_B")

    # Step 1: Worker A acquires lease (gen 1)
    ok, reason, gen_a = worker_a.registry.acquire_lease(
        f"lease:{run_id}", f"run:{run_id}:generation", "runs:active", "worker_A", 10, run_id
    )
    assert ok is True
    assert gen_a == 1

    # Worker A writes meta with generation 1
    worker_a.registry.fenced_hset(
        f"run:{run_id}:generation", f"run:{run_id}:meta", gen_a, {"status": "running"}
    )

    # Step 2: Worker A stalls / lease expires. Worker B preempts with higher generation (gen 2)
    ok, reason, gen_b = worker_b.registry.acquire_lease(
        f"lease:{run_id}", f"run:{run_id}:generation", "runs:active", "worker_B", 15, run_id, force=True
    )
    assert ok is True
    assert gen_b == 2

    # Step 3: Stale Worker A wakes up and attempts fenced write -> MUST BE REJECTED
    write_ok, write_reason = worker_a.registry.fenced_hset(
        f"run:{run_id}:generation", f"run:{run_id}:meta", gen_a, {"status": "stale_write"}
    )
    assert write_ok is False
    assert write_reason == "stale_generation"

    # Step 4: Stale Worker A attempts commit pointer advance -> MUST BE REJECTED
    commit_ok, commit_reason = worker_a.registry.advance_commit(
        f"run:{run_id}:generation",
        f"run:{run_id}:committed",
        generation=gen_a,
        seq=10,
        committed_payload={"checkpoint_id": "stale_cp", "generation": gen_a, "seq": 10},
    )
    assert commit_ok is False
    assert commit_reason == "stale_generation"

    # Step 5: Worker B executes and completes the run
    final_state = worker_b.execute_run(
        run_id=run_id,
        initial_question="Resumed Question?",
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )
    assert final_state.status == "completed"


def test_stalled_worker_max_resume_attempts_exceeded():
    from scout.store.redis_keys import key_runs_active, key_run_meta

    run_id = f"stalled_max_{uuid.uuid4().hex[:8]}"
    r = redis.Redis.from_url("redis://127.0.0.1:6379/0")

    # Setup stalled run with resume_attempts = 3
    r.sadd(key_runs_active(), run_id)
    r.hset(
        key_run_meta(run_id),
        mapping={
            "run_id": run_id,
            "status": "running",
            "question": "Dead run question",
            "resume_attempts": "3",
        },
    )

    worker = ResearchWorker(worker_id="test_worker_max")
    resumed = worker.scan_and_resume_stalled_runs()

    # The run should not have been resumed
    assert run_id not in resumed

    # The run should be marked failed with max_resume_attempts_exceeded
    meta = {k.decode(): v.decode() for k, v in r.hgetall(key_run_meta(run_id)).items()}
    assert meta["status"] == "failed"
    assert meta["termination_reason"] == "max_resume_attempts_exceeded"

    # And removed from active runs
    active = [x.decode() for x in r.smembers(key_runs_active())]
    assert run_id not in active
