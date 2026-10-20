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


def test_structured_output_parser_conversational_wrapper():
    client = LLMClient()
    chat_text = 'Here is the requested analysis:\n```json\n{"summary": "Wrapped summary"}\n```\nLet me know if you need more.'
    parsed = client._parse_structured_output(chat_text, SampleModel)
    assert parsed.summary == "Wrapped summary"


def test_structured_output_parser_embedded_braces():
    client = LLMClient()
    raw = 'Sure thing! The data is {"summary": "Direct braces"} thank you!'
    parsed = client._parse_structured_output(raw, SampleModel)
    assert parsed.summary == "Direct braces"


def test_structured_output_parser_extraction_no_findings():
    from scout.agents.researcher import ExtractionOutput
    client = LLMClient()
    refusal = "No relevant findings or evidence were found in these passages."
    parsed = client._parse_structured_output(refusal, ExtractionOutput)
    assert len(parsed.findings) == 0
