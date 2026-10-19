import pytest
from pydantic import BaseModel

from scout.llm import LLMClient, TokenBudgetExceededError


class SampleModel(BaseModel):
    summary: str


def test_token_reservation_exceeded():
    client = LLMClient()
    # Attempting to call with 1 remaining token must raise TokenBudgetExceededError
    with pytest.raises(TokenBudgetExceededError):
        client.generate(
            role="planner",
            messages=[{"role": "user", "content": "Hello world"}],
            remaining_token_budget=1,
        )


def test_structured_output_parser():
    client = LLMClient()
    raw_markdown = '```json\n{"summary": "Test summary"}\n```'
    parsed = client._parse_structured_output(raw_markdown, SampleModel)
    assert parsed.summary == "Test summary"
