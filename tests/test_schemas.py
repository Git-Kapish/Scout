import pytest
from pydantic import ValidationError

from scout.schemas import (
    Claim,
    Finding,
    Plan,
    ResearchResult,
    SubQuestion,
)


def test_plan_unique_subquestion_ids():
    sq1 = SubQuestion(id="sq_1", question="What is X?", rationale="Rationale 1")
    sq2 = SubQuestion(id="sq_1", question="What is Y?", rationale="Rationale 2")
    with pytest.raises(ValidationError):
        Plan(sub_questions=[sq1, sq2])


def test_claim_requires_at_least_one_finding_id():
    with pytest.raises(ValidationError):
        Claim(text="Unsupported assertion", finding_ids=[])

    valid_claim = Claim(text="Supported assertion", finding_ids=["find_123"])
    assert valid_claim.finding_ids == ["find_123"]


def test_finding_and_research_result():
    f = Finding(
        finding_id="find_1",
        subquestion_id="sq_1",
        claim="RAGAS measures faithfulness",
        snippet="faithfulness measures the factual consistency",
        source_url="https://ragas.io",
        domain="ragas.io",
        page_hash="abc123hash",
        verified=True,
    )
    assert f.verified is True

    res = ResearchResult(
        subquestion_id="sq_1",
        status="ok",
        finding_ids=["find_1"],
        searches_used=1,
        pages_fetched=1,
    )
    assert res.status == "ok"
    assert len(res.finding_ids) == 1
