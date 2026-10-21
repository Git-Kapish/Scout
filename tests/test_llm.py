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


def _fake_openai(content: str = "ok"):
    """Minimal stand-in for the OpenAI client that counts dispatches."""
    from types import SimpleNamespace

    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


def test_meter_counts_tokens_and_refuses_dispatch_over_call_cap(monkeypatch):
    from scout.effects.runtime import BudgetExhaustedError, DispatchMeter, metered

    fake, calls = _fake_openai()
    client = LLMClient()
    monkeypatch.setattr(client, "_create_client", lambda provider: (fake, "url"))
    msgs = [{"role": "user", "content": "hi"}]

    meter = DispatchMeter(max_llm_calls=2, max_tokens=10**6)
    with metered(meter):
        client.generate("planner", msgs)
        client.generate("planner", msgs)
        with pytest.raises(BudgetExhaustedError, match="llm_calls_exhausted"):
            client.generate("planner", msgs)

    assert calls["n"] == 2
    assert (meter.llm_calls, meter.tokens_in, meter.tokens_out) == (2, 200, 40)


def test_meter_refuses_dispatch_when_token_reservation_exceeds_cap(monkeypatch):
    from scout.effects.runtime import BudgetExhaustedError, DispatchMeter, metered

    fake, calls = _fake_openai()
    client = LLMClient()
    monkeypatch.setattr(client, "_create_client", lambda provider: (fake, "url"))

    with metered(DispatchMeter(max_llm_calls=10, max_tokens=1000)):  # below prompt + max_tokens reservation
        with pytest.raises(BudgetExhaustedError, match="token_budget_exhausted"):
            client.generate("planner", [{"role": "user", "content": "hi"}])
    assert calls["n"] == 0


def test_replay_miss_fails_closed_without_dispatch(monkeypatch, tmp_path):
    import scout.effects.runtime as runtime
    from scout.config import get_settings
    from scout.effects import EffectRecorder, ReplayMissError

    monkeypatch.setattr(get_settings(), "scout_mode", "replay")
    monkeypatch.setattr(runtime, "_recorder", EffectRecorder(snapshot_dir=tmp_path))
    client = LLMClient()

    def no_live_client(provider):
        raise AssertionError("replay mode must not create a live client")

    monkeypatch.setattr(client, "_create_client", no_live_client)
    with pytest.raises(ReplayMissError):
        client.generate("planner", [{"role": "user", "content": "hi"}])
