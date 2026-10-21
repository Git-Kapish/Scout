"""Tests for Mechanical Citation Resolution, Regeneration, and Markdown Renderer (PRD FR-5.4, §13).

Covers:
1. Mechanical citation resolution: every claim cites >= 1 verified finding.
2. Injected bad citations (unknown ID, unverified finding, empty IDs) are rejected.
3. Writer regeneration loop with validation feedback up to max_regenerations.
4. Pruning of unresolved claims after max regenerations with evidence re-check:
   - Sufficient evidence remaining -> completed_partial with re-rendered markdown.
   - Insufficient evidence remaining -> failed with insufficient_evidence, report=None.
5. Code-based deterministic Markdown rendering with sequential references.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from scout.agents.planner import Plan
from scout.agents.writer import WriterAgent
from scout.graph.budget_guard import check_minimum_evidence_for_report
from scout.graph.nodes import validate_citations_node, write_node
from scout.graph.state import ResearchState
from scout.schemas import Claim, Finding, Report, ReportSection, SubQuestion
from scout.validate.citation_validator import validate_report_citations
from scout.validate.markdown_renderer import render_markdown_report


@pytest.fixture
def sample_findings():
    dt = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
    return {
        "f_1": Finding(
            finding_id="f_1",
            subquestion_id="sq_1",
            claim="Transformers scale with compute.",
            snippet="Transformers exhibit power-law scaling with compute and dataset size.",
            source_url="https://arxiv.org/abs/2001.08361",
            domain="arxiv.org",
            title="Scaling Laws for Neural Language Models",
            retrieved_at=dt,
            page_hash="h1",
            verified=True,
        ),
        "f_2": Finding(
            finding_id="f_2",
            subquestion_id="sq_1",
            claim="Inference costs decrease over time.",
            snippet="Quantization reduces inference memory bandwidth requirements significantly.",
            source_url="https://nature.com/articles/s41586-024",
            domain="nature.com",
            title="Efficient Deep Learning Inference",
            retrieved_at=dt,
            page_hash="h2",
            verified=True,
        ),
        "f_3": Finding(
            finding_id="f_3",
            subquestion_id="sq_2",
            claim="Agentic workflows improve reasoning.",
            snippet="Multi-agent architectures achieve superior benchmark scores through iterative review.",
            source_url="https://ai.google/research/agent-review",
            domain="ai.google",
            title="Iterative Agentic Review Systems",
            retrieved_at=dt,
            page_hash="h3",
            verified=True,
        ),
        "f_4": Finding(
            finding_id="f_4",
            subquestion_id="sq_2",
            claim="Fencing prevents split-brain writes.",
            snippet="Monotonic generation fencing guarantees safe crash recovery under network partitions.",
            source_url="https://redis.io/topics/fencing",
            domain="redis.io",
            title="Distributed Fencing Tokens in Redis",
            retrieved_at=dt,
            page_hash="h4",
            verified=True,
        ),
        "f_unverified": Finding(
            finding_id="f_unverified",
            subquestion_id="sq_2",
            claim="Unverified claim",
            snippet="Snippet that failed verbatim check",
            source_url="https://unverified.org/page",
            domain="unverified.org",
            title="Unverified Source",
            retrieved_at=dt,
            page_hash="h_bad",
            verified=False,
        ),
    }


def test_valid_citation_resolution(sample_findings):
    """Clean report with all valid verified citations passes mechanical validation."""
    report = Report(
        title="Valid Research",
        summary="A summary",
        sections=[
            ReportSection(
                title="Scaling",
                content="Discussion of scaling.",
                claims=[
                    Claim(text="Transformers scale with compute.", finding_ids=["f_1", "f_2"]),
                ],
            ),
            ReportSection(
                title="Systems",
                content="Discussion of systems.",
                claims=[
                    Claim(text="Agents use fencing.", finding_ids=["f_3", "f_4"]),
                ],
            ),
        ],
    )

    val = validate_report_citations(report, sample_findings)
    assert val.valid is True
    assert len(val.unresolved_citations) == 0
    assert len(val.dropped_claims) == 0


def test_injected_bad_citations_rejected(sample_findings):
    """PRD Acceptance Criteria: Citation-resolution regression tests pass (injected bad citations rejected)."""
    bad_report = Report(
        title="Bad Citations",
        summary="A summary",
        sections=[
            ReportSection(
                title="Faulty Citations",
                content="Text with bogus citations.",
                claims=[
                    # Case 1: Non-existent / hallucinated finding ID
                    Claim(text="Hallucinated fact.", finding_ids=["f_hallucinated_999"]),
                    # Case 2: Unverified finding ID
                    Claim(text="Unverified fact.", finding_ids=["f_unverified"]),
                    # Case 3: Mixed valid and invalid IDs
                    Claim(text="Mixed fact.", finding_ids=["f_1", "f_nonexistent"]),
                ],
            )
        ],
    )

    val = validate_report_citations(bad_report, sample_findings)
    assert val.valid is False
    assert len(val.unresolved_citations) == 3
    assert len(val.dropped_claims) == 3

    unresolved_str = " ".join(val.unresolved_citations)
    assert "f_hallucinated_999" in unresolved_str
    assert "f_unverified" in unresolved_str
    assert "f_nonexistent" in unresolved_str


def test_empty_citation_label_rejected(sample_findings):
    """Claims with no citations are rejected."""
    bad_report = Report(
        title="Empty Citations",
        summary="Summary",
        sections=[
            ReportSection(
                title="Section",
                content="Content",
                claims=[
                    Claim.model_construct(text="Unsupported assertion.", finding_ids=[]),
                ],
            )
        ],
    )

    val = validate_report_citations(bad_report, sample_findings)
    assert val.valid is False
    assert any("empty finding_ids" in u for u in val.unresolved_citations)


def test_markdown_renderer_formatting(sample_findings):
    """PRD FR-5.4: Markdown rendering with references list is produced by code from validated structure."""
    sections = [
        ReportSection(
            title="Foundation Models",
            content="Recent findings on model scaling.",
            claims=[
                Claim(text="Transformers scale with compute.", finding_ids=["f_1"]),
                Claim(text="Inference costs decrease over time.", finding_ids=["f_2"]),
            ],
        ),
        ReportSection(
            title="Agent Architecture",
            content="Stateful agent supervision.",
            claims=[
                Claim(text="Fencing prevents split-brain writes.", finding_ids=["f_4"]),
            ],
        ),
    ]

    md = render_markdown_report(
        title="State of Agentic AI",
        summary="Overview of scaling and durability.",
        sections=sections,
        findings_registry=sample_findings,
        coverage_gaps=["sq_3: Edge deployment"],
    )

    # Invariants
    assert "# State of Agentic AI" in md
    assert "**Executive Summary:** Overview of scaling and durability." in md
    assert "## Foundation Models" in md
    assert "- Transformers scale with compute. [1]" in md
    assert "- Inference costs decrease over time. [2]" in md
    assert "## Agent Architecture" in md
    assert "- Fencing prevents split-brain writes. [3]" in md
    assert "## Coverage Gaps" in md
    assert "- sq_3: Edge deployment" in md
    assert "## References" in md
    assert "[1] **Scaling Laws for Neural Language Models** - [https://arxiv.org/abs/2001.08361]" in md
    assert "*Quote: \"Transformers exhibit power-law scaling with compute and dataset size.\"" in md
    assert "[2] **Efficient Deep Learning Inference** - [https://nature.com/articles/s41586-024]" in md
    assert "[3] **Distributed Fencing Tokens in Redis** - [https://redis.io/topics/fencing]" in md


def test_writer_regeneration_feedback_and_pruning_flow(sample_findings):
    """PRD FR-5.4: On failure, regenerate with validation feedback up to max_regenerations.

    If it still fails, remove unresolved claims, flag them, and re-check minimum evidence.
    """
    plan = Plan(
        schema_version="v1",
        sub_questions=[
            SubQuestion(id="sq_1", question="What is scaling?", rationale="r1"),
            SubQuestion(id="sq_2", question="What is fencing?", rationale="r2"),
        ],
    )

    # Initial report with injected bad citation
    bad_report = Report(
        title="Initial Report",
        summary="Summary",
        sections=[
            ReportSection(
                title="Sec 1",
                content="Content",
                claims=[
                    Claim(text="Valid claim 1", finding_ids=["f_1"]),
                    Claim(text="Valid claim 2", finding_ids=["f_2"]),
                    Claim(text="Valid claim 3", finding_ids=["f_3"]),
                    Claim(text="Valid claim 4", finding_ids=["f_4"]),
                    Claim(text="Bogus claim", finding_ids=["f_bogus"]),
                ],
            )
        ],
    )

    state = ResearchState(
        run_id="run_test_regen",
        question="What is scaling and fencing?",
        plan=plan,
        findings=list(sample_findings.values())[:4],  # 4 verified findings
        report=bad_report,
        completed_subquestion_ids=["sq_1", "sq_2"],
        writer_regenerations=0,
    )

    # Attempt 1: Citation validation triggers regeneration (attempt 1/2)
    res1 = validate_citations_node(state)
    assert res1["writer_regenerations"] == 1
    assert res1["validation_result"].valid is False
    assert len(res1["validation_result"].dropped_claims) == 1

    # Verify write_node passes validation feedback to writer
    mock_writer = MagicMock()
    mock_writer.write.return_value = bad_report
    state.validation_result = res1["validation_result"]
    state.writer_regenerations = 1

    write_node(state, mock_writer)
    assert mock_writer.write.called
    kwargs = mock_writer.write.call_args.kwargs
    assert kwargs["validation_feedback"] is not None
    assert "Unknown finding ID: f_bogus" in kwargs["validation_feedback"]

    # Attempt 2: Still fails after max_regenerations (set regenerations to 2)
    state.writer_regenerations = 2
    res2 = validate_citations_node(state)

    # Max regenerations exhausted: bad claim pruned, evidence re-checked
    assert "report" in res2
    pruned_report = res2["report"]
    all_claims = [c for s in pruned_report.sections for c in s.claims]
    assert len(all_claims) == 4  # Bogus claim was removed
    assert not any("f_bogus" in c.finding_ids for c in all_claims)
    # Never return an unresolved citation
    assert "f_bogus" not in pruned_report.markdown


def test_evidence_recheck_fails_when_pruning_drops_below_minimum(sample_findings):
    """PRD FR-5.4 & FR-2.4: If claim removal drops evidence below threshold, end as failed with insufficient_evidence."""
    plan = Plan(
        schema_version="v1",
        sub_questions=[
            SubQuestion(id="sq_1", question="Q1", rationale="r1"),
            SubQuestion(id="sq_2", question="Q2", rationale="r2"),
        ],
    )

    # Report has ONLY bad citations that will all be pruned
    bad_report = Report(
        title="Report to be emptied",
        summary="Summary",
        sections=[
            ReportSection(
                title="Sec 1",
                content="All bad claims",
                claims=[
                    Claim(text="Bogus 1", finding_ids=["f_fake1"]),
                    Claim(text="Bogus 2", finding_ids=["f_fake2"]),
                ],
            )
        ],
    )

    state = ResearchState(
        run_id="run_test_fail_evidence",
        question="A question",
        plan=plan,
        findings=list(sample_findings.values())[:4],
        report=bad_report,
        completed_subquestion_ids=["sq_1", "sq_2"],
        writer_regenerations=2,  # exhausted
    )

    res = validate_citations_node(state)
    assert res["status"] == "failed"
    assert res["termination_reason"] == "insufficient_evidence"
    assert res["report"] is None


def test_writer_prompt_findings_capping_with_large_finding_set():
    """Verify that WriterAgent caps findings passed to the prompt to 12 when there are >12 findings."""
    from scout.agents.writer import WriterAgent
    from scout.schemas import Finding, Plan, SubQuestion

    mock_llm = MagicMock()
    # Mock LLM returns a simple valid report referencing finding f_1
    mock_llm.generate.return_value = MagicMock(
        parsed=Report(
            title="Title",
            summary="Summary",
            sections=[
                ReportSection(
                    title="Sec",
                    content="Content",
                    claims=[Claim(text="Claim", finding_ids=["f_1"])],
                )
            ],
            markdown="# Report",
        ),
        usage=MagicMock(total_tokens=100),
    )

    writer = WriterAgent(llm_client=mock_llm)
    plan = Plan(
        schema_version="v1",
        sub_questions=[
            SubQuestion(id="sq_1", question="Q1", rationale="r1"),
            SubQuestion(id="sq_2", question="Q2", rationale="r2"),
            SubQuestion(id="sq_3", question="Q3", rationale="r3"),
        ],
    )

    # 30 findings across 3 subquestions
    many_findings = []
    for i in range(1, 31):
        sq_id = f"sq_{(i % 3) + 1}"
        many_findings.append(
            Finding(
                finding_id=f"f_{i}",
                subquestion_id=sq_id,
                claim=f"Claim number {i}",
                snippet=f"Verbatim snippet {i}",
                source_url=f"https://source{i}.org",
                domain=f"source{i}.org",
                page_hash=f"hash_{i}",
                verified=True,
            )
        )

    report = writer.write(
        question="Large findings test",
        plan=plan,
        findings=many_findings,
    )

    assert report is not None
    # Inspect the prompt passed to mock_llm.generate
    call_args = mock_llm.generate.call_args
    messages = call_args.kwargs["messages"]
    user_prompt = messages[1]["content"]

    # Verify prompt contains at most 12 finding ID entries
    import re
    prompt_f_ids = re.findall(r"- ID: (f_\d+)", user_prompt)
    assert len(prompt_f_ids) <= 12
    assert len(prompt_f_ids) > 0


def test_report_without_claims_is_invalid():
    report = Report(title="T", summary="S", sections=[ReportSection(title="S", content="Uncited prose.", claims=[])])
    result = validate_report_citations(report, {})
    assert result.valid is False
    assert "Report contains no cited claims" in result.unresolved_citations
