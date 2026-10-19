"""Graph Topology Integration Tests (PRD FR-2.1 - FR-2.4, §13).

Validates path coverage using deterministic fake providers:
1. Accept path -> completed
2. Rework-once path -> completed with total_reworks == 1
3. Abandon path -> completed_partial with coverage gaps
4. Insufficient evidence path -> failed with termination_reason == 'insufficient_evidence'
5. Budget breach path -> hard caps never exceeded
6. Cancellation path -> cancelled
"""

from __future__ import annotations

from unittest.mock import MagicMock

from scout.agents.planner import PlannerAgent, PlannerOutputSchema
from scout.agents.researcher import (
    CandidateFinding,
    ExtractionOutput,
    QueryGenerationOutput,
    ResearcherAgent,
)
from scout.agents.writer import WriterAgent, WriterOutputSchema, WriterSectionOutput
from scout.fetch.fetcher import PageFetchResult
from scout.graph.builder import create_research_graph
from scout.graph.state import ResearchState
from scout.graph.supervisor import SupervisorReviewer
from scout.llm import LLMResponse, LLMUsage
from scout.schemas import (
    Claim,
    Finding,
    ReviewDecision,
    SubQuestion,
)
from scout.search import FixtureSearchAdapter, SearchChain, extract_domain


def make_mock_llm_resp(parsed) -> LLMResponse:
    return LLMResponse(
        content=str(parsed),
        parsed=parsed,
        provider="fake",
        model="fake-model",
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        latency_s=0.01,
    )


def build_mock_harness(
    subquestions: list[SubQuestion],
    research_pages: dict[str, str],
    review_decisions: list[ReviewDecision] | None = None,
    candidate_findings: list[CandidateFinding] | None = None,
):
    """Constructs deterministic fake agents for topology testing."""
    # 1. Planner
    mock_planner_llm = MagicMock()
    mock_planner_llm.estimate_prompt_tokens.return_value = 50
    mock_planner_llm.generate.return_value = make_mock_llm_resp(
        PlannerOutputSchema(sub_questions=subquestions)
    )
    planner = PlannerAgent(llm_client=mock_planner_llm)

    # 2. Search & Fetcher
    search_adapter = FixtureSearchAdapter()
    for q in ["query_1", "query_2", "targeted research query"]:
        hits = [
            {"title": f"Doc for {url}", "url": url, "snippet": "snippet", "domain": extract_domain(url)}
            for url in research_pages.keys()
        ]
        search_adapter.register_fixture(q, hits)
    search_chain = SearchChain(providers=[search_adapter])

    mock_fetcher = MagicMock()

    def fetch_fn(url: str):
        if url in research_pages:
            return PageFetchResult(
                url=url,
                status="ok",
                text_content=research_pages[url],
                page_hash=f"hash_{abs(hash(url))}",
                status_code=200,
            )
        return PageFetchResult(url=url, status="no_results", status_code=404)

    mock_fetcher.fetch.side_effect = fetch_fn

    # 3. Researcher
    mock_researcher_llm = MagicMock()
    mock_researcher_llm.estimate_prompt_tokens.return_value = 50

    findings_pool = candidate_findings or []

    def researcher_generate(role, messages, response_model=None, **kwargs):
        if response_model == QueryGenerationOutput:
            return make_mock_llm_resp(QueryGenerationOutput(queries=["targeted research query"]))
        elif response_model == ExtractionOutput:
            return make_mock_llm_resp(ExtractionOutput(findings=findings_pool))
        return make_mock_llm_resp(None)

    mock_researcher_llm.generate.side_effect = researcher_generate
    researcher = ResearcherAgent(
        llm_client=mock_researcher_llm,
        search_chain=search_chain,
        fetcher=mock_fetcher,
    )

    # 4. Supervisor Reviewer
    mock_supervisor_llm = MagicMock()
    mock_supervisor_llm.estimate_prompt_tokens.return_value = 50
    if review_decisions:
        mock_supervisor_llm.generate.side_effect = [make_mock_llm_resp(d) for d in review_decisions]
    supervisor = SupervisorReviewer(llm_client=mock_supervisor_llm)

    # 5. Writer
    mock_writer_llm = MagicMock()
    mock_writer_llm.estimate_prompt_tokens.return_value = 50

    def writer_generate(role, messages, response_model=None, **kwargs):
        # Dynamically create valid citations from prompt
        return make_mock_llm_resp(
            WriterOutputSchema(
                title="Synthesized Topology Report",
                summary="Executive overview.",
                sections=[
                    WriterSectionOutput(
                        title="Section 1",
                        content="Findings content.",
                        claims=[],
                    )
                ],
            )
        )

    mock_writer_llm.generate.side_effect = writer_generate
    writer = WriterAgent(llm_client=mock_writer_llm)

    return planner, researcher, supervisor, writer


def test_topology_happy_path_accept():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    pages = {
        "https://test.org/page1": "Page 1 contains key fact alpha verified verbatim.",
        "https://test2.org/page2": "Page 2 contains key fact beta verified verbatim.",
    }
    candidates = [
        CandidateFinding(claim="Fact alpha", snippet="key fact alpha verified verbatim"),
        CandidateFinding(claim="Fact beta", snippet="key fact beta verified verbatim"),
    ]

    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages=pages,
        candidate_findings=candidates,
    )

    # Force writer to output claims matching verified findings
    def valid_writer(question, plan, findings, **kwargs):
        from scout.schemas import Report, ReportSection
        fids = [f.finding_id for f in findings]
        return Report(
            title="Valid Report",
            summary="Valid Summary",
            sections=[
                ReportSection(
                    title="Section 1",
                    content="Content",
                    claims=[Claim(text="Claim A", finding_ids=fids[:2])],
                )
            ],
            coverage_gaps=[],
            markdown="# Valid Report\n\n- Claim A [1]",
        )

    writer.write = MagicMock(side_effect=valid_writer)

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    initial_state = ResearchState(
        run_id="run_happy_path",
        question="What are A and B?",
    )

    final_state = app.invoke(initial_state)

    assert final_state["status"] == "completed"
    assert len(final_state["completed_subquestion_ids"]) == 2
    assert final_state["report"] is not None
    assert final_state["validation_result"].valid is True


def test_topology_rework_once():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    pages = {
        "https://test.org/page1": "Page 1 contains key fact alpha verified verbatim for question.",
        "https://test2.org/page2": "Page 2 contains key fact beta verified verbatim for question.",
    }

    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages=pages,
        candidate_findings=[
            CandidateFinding(claim="Fact alpha", snippet="key fact alpha verified verbatim"),
            CandidateFinding(claim="Fact beta", snippet="key fact beta verified verbatim"),
        ],
    )

    # Reviewer forces rework on first call, then accepts
    review_calls = 0

    def mock_review(sq, subq_findings, state):
        nonlocal review_calls
        review_calls += 1
        if sq.id == "sq_1" and review_calls == 1:
            return ReviewDecision(decision="rework", missing=["detail"], reason="First pass missing detail.")
        return ReviewDecision(decision="accept", reason="Accepted.")

    supervisor.review = MagicMock(side_effect=mock_review)

    # Valid writer
    def valid_writer(question, plan, findings, **kwargs):
        from scout.schemas import Report, ReportSection
        fids = [f.finding_id for f in findings]
        return Report(
            title="Reworked Report",
            summary="Summary",
            sections=[ReportSection(title="S1", content="C", claims=[Claim(text="Claim", finding_ids=fids[:2])])],
            coverage_gaps=[],
            markdown="# Report",
        )

    writer.write = MagicMock(side_effect=valid_writer)

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    initial_state = ResearchState(
        run_id="run_rework_test",
        question="Test rework?",
    )

    final_state = app.invoke(initial_state)

    assert final_state["status"] == "completed"
    assert final_state["total_reworks"] == 1
    assert final_state["rework_counts"]["sq_1"] == 1


def test_topology_abandon_leads_to_completed_partial():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    # Page 1 has facts for sq_1; page 2 is missing for sq_2
    pages = {
        "https://test.org/p1": "Fact one is here.",
        "https://test2.org/p2": "Fact two is here.",
        "https://test3.org/p3": "Fact three is here.",
        "https://test4.org/p4": "Fact four is here.",
    }

    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages=pages,
        candidate_findings=[CandidateFinding(claim="Fact", snippet="Fact one is here.")],
    )

    # sq_1 accepted with 4 findings from 2 domains; sq_2 abandoned
    def mock_review(sq, subq_findings, state):
        if sq.id == "sq_2":
            return ReviewDecision(decision="abandon", reason="No data exists.")
        return ReviewDecision(decision="accept", reason="Accepted.")

    supervisor.review = MagicMock(side_effect=mock_review)

    # Supply 4 verified findings for sq_1 from 2 domains to satisfy minimum evidence
    def mock_research(sq, **kwargs):
        from scout.schemas import ResearchResult
        if sq.id == "sq_1":
            findings = [
                Finding(finding_id=f"f{i}", subquestion_id="sq_1", claim="C", snippet="S", source_url=f"https://d{i%2}.org", domain=f"d{i%2}.org", page_hash="h", verified=True)
                for i in range(4)
            ]
            return ResearchResult(subquestion_id="sq_1", status="ok", finding_ids=[f.finding_id for f in findings]), findings
        return ResearchResult(subquestion_id="sq_2", status="no_results"), []

    researcher.execute_subquestion = MagicMock(side_effect=mock_research)

    def valid_writer(question, plan, findings, abandoned_subquestions=None, **kwargs):
        from scout.schemas import Report, ReportSection
        fids = [f.finding_id for f in findings]
        return Report(
            title="Partial Report",
            summary="Summary",
            sections=[ReportSection(title="S", content="C", claims=[Claim(text="C", finding_ids=fids[:2])])],
            coverage_gaps=abandoned_subquestions or [],
            markdown="# Partial",
        )

    writer.write = MagicMock(side_effect=valid_writer)

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    final_state = app.invoke(ResearchState(run_id="run_abandon_test", question="Test?"))

    assert final_state["status"] == "completed_partial"
    assert "sq_2" in final_state["abandoned_subquestion_ids"]
    assert "sq_2" in final_state["report"].coverage_gaps


def test_topology_insufficient_evidence_fails():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages={},  # No pages, total search failure
        candidate_findings=[],
    )

    def mock_review(sq, subq_findings, state):
        return ReviewDecision(decision="abandon", reason="Search miss.")

    supervisor.review = MagicMock(side_effect=mock_review)

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    final_state = app.invoke(ResearchState(run_id="run_fail_test", question="Test?"))

    assert final_state["status"] == "failed"
    assert final_state["termination_reason"] == "insufficient_evidence"
    assert final_state["report"] is None
    assert "diagnostics" in final_state
    assert final_state["diagnostics"]["total_failures"] >= 0


def test_topology_cancellation():
    subqs = [SubQuestion(id="sq_1", question="A?", rationale="r")]
    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages={},
    )

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    # Cancelled before graph runs
    state = ResearchState(
        run_id="run_cancel_test",
        question="Test?",
        cancellation_requested=True,
    )

    final_state = app.invoke(state)
    assert final_state["status"] == "cancelled"
    assert final_state["termination_reason"] == "cancelled"


def test_adversarial_budget_limits_respected():
    subqs = [SubQuestion(id=f"sq_{i}", question=f"SubQ {i}", rationale="r") for i in range(5)]
    planner, researcher, supervisor, writer = build_mock_harness(
        subquestions=subqs,
        research_pages={},
    )

    app = create_research_graph(
        planner_agent=planner,
        researcher_agent=researcher,
        writer_agent=writer,
        supervisor_reviewer=supervisor,
    )

    # Restrict total LLM calls to 2 (adversarial low budget)
    state = ResearchState(
        run_id="run_adversarial_budget",
        question="Test?",
        options={"max_llm_calls": 2},
    )

    final_state = app.invoke(state)

    # Must halt without exceeding call ceiling
    assert final_state["budget_ledger"].llm_calls <= 2
    assert final_state["status"] == "failed"
    assert final_state["termination_reason"] in ("llm_calls_exhausted", "insufficient_evidence")
