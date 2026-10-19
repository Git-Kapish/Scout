from unittest.mock import MagicMock

from scout.agents.planner import PlannerAgent, PlannerOutputSchema
from scout.agents.researcher import (
    CandidateFinding,
    ExtractionOutput,
    QueryGenerationOutput,
    ResearcherAgent,
)
from scout.agents.writer import WriterAgent, WriterOutputSchema, WriterSectionOutput
from scout.llm import LLMResponse, LLMUsage
from scout.schemas import Claim, Finding, Plan, SubQuestion
from scout.search import FixtureSearchAdapter, SearchChain


def make_mock_response(parsed_obj) -> LLMResponse:
    return LLMResponse(
        content=str(parsed_obj),
        parsed=parsed_obj,
        provider="mock",
        model="mock-model",
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        latency_s=0.1,
    )


def test_planner_agent_isolation():
    mock_llm = MagicMock()
    mock_subqs = [
        SubQuestion(id="sq_1", question="What is Redis AOF?", rationale="Understand AOF"),
        SubQuestion(id="sq_2", question="What is Redis RDB?", rationale="Understand RDB"),
    ]
    mock_llm.generate.return_value = make_mock_response(
        PlannerOutputSchema(sub_questions=mock_subqs)
    )

    planner = PlannerAgent(llm_client=mock_llm)
    plan = planner.plan("Compare Redis AOF and RDB", max_subquestions=2)

    assert len(plan.sub_questions) == 2
    assert plan.sub_questions[0].id == "sq_1"
    assert plan.sub_questions[1].id == "sq_2"


def test_researcher_agent_isolation(tmp_path):
    mock_llm = MagicMock()

    # Query generation response
    query_resp = make_mock_response(QueryGenerationOutput(queries=["redis aof fsync"]))

    # Extraction response with 1 valid snippet and 1 hallucinated snippet (triggers 1 retry)
    extract_resp = make_mock_response(
        ExtractionOutput(
            findings=[
                CandidateFinding(
                    claim="AOF fsync occurs every second",
                    snippet="AOF synchronizes writes every second via background threads",
                ),
                CandidateFinding(
                    claim="Hallucinated fact",
                    snippet="This snippet definitely does not appear in the page text",
                ),
            ]
        )
    )

    # Retry response fixing the hallucination
    retry_extract_resp = make_mock_response(
        ExtractionOutput(
            findings=[
                CandidateFinding(
                    claim="AOF fsync occurs every second",
                    snippet="AOF synchronizes writes every second via background threads",
                )
            ]
        )
    )

    mock_llm.generate.side_effect = [query_resp, extract_resp, retry_extract_resp]

    # Mock search with fixture
    fixture_search = FixtureSearchAdapter()
    fixture_search.register_fixture(
        "redis aof fsync",
        [
            {
                "title": "Redis Docs",
                "url": "https://redis.io/docs/persistence",
                "snippet": "AOF persistence",
                "domain": "redis.io",
            }
        ],
    )
    search_chain = SearchChain(providers=[fixture_search])

    # Mock fetcher
    mock_fetcher = MagicMock()
    from scout.fetch import PageFetchResult

    page_text = (
        "Redis Persistence Overview.\n\n"
        "AOF synchronizes writes every second via background threads to prevent latency spikes.\n\n"
        "RDB creates point-in-time snapshots."
    )
    mock_fetcher.fetch.return_value = PageFetchResult(
        url="https://redis.io/docs/persistence",
        status="ok",
        text_content=page_text,
        page_hash="dummyhash",
        status_code=200,
    )

    researcher = ResearcherAgent(
        llm_client=mock_llm,
        search_chain=search_chain,
        fetcher=mock_fetcher,
    )

    sq = SubQuestion(id="sq_1", question="How does Redis AOF fsync work?", rationale="Explore AOF")
    result, findings = researcher.execute_subquestion(sq)

    assert result.status in ("ok", "partial")
    # Verifies that hallucinated finding was dropped mechanically (PRD FR-3.3)
    assert len(findings) == 1
    assert findings[0].claim == "AOF fsync occurs every second"
    assert findings[0].verified is True


def test_writer_agent_isolation():
    mock_llm = MagicMock()
    mock_report = WriterOutputSchema(
        title="Redis Persistence Architecture",
        summary="Overview of AOF and RDB trade-offs.",
        sections=[
            WriterSectionOutput(
                title="AOF Mechanics",
                content="AOF logs every write command sequentially.",
                claims=[
                    Claim(text="AOF logs writes sequentially", finding_ids=["find_aof_1"])
                ],
            )
        ],
    )
    mock_llm.generate.return_value = make_mock_response(mock_report)

    writer = WriterAgent(llm_client=mock_llm)

    plan = Plan(
        sub_questions=[
            SubQuestion(id="sq_1", question="What is AOF?", rationale="r1"),
            SubQuestion(id="sq_2", question="What is RDB?", rationale="r2"),
        ]
    )
    findings = [
        Finding(
            finding_id="find_aof_1",
            subquestion_id="sq_1",
            claim="AOF logs writes sequentially",
            snippet="AOF logs every write command sequentially",
            source_url="https://redis.io",
            domain="redis.io",
            page_hash="hash1",
            verified=True,
        )
    ]

    report = writer.write(
        question="Explain Redis persistence",
        plan=plan,
        findings=findings,
    )

    assert report.title == "Redis Persistence Architecture"
    assert len(report.sections) == 1
    assert report.sections[0].claims[0].finding_ids == ["find_aof_1"]

    # Verify deterministic coverage gaps (sq_2 had no findings)
    assert len(report.coverage_gaps) >= 1
    assert any("sq_2" in gap for gap in report.coverage_gaps)

    # Verify Markdown rendering
    assert "# Redis Persistence Architecture" in report.markdown
    assert "find_aof_1" in report.markdown or "[1]" in report.markdown
    assert "https://redis.io" in report.markdown
