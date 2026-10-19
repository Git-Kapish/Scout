import pytest

from scout.effects import (
    EffectRecorder,
    ReplayMissError,
    compute_effect_key,
    maybe_crash,
    reset_fault_counters,
)


def test_effect_key_stability():
    key1 = compute_effect_key("llm", "planner", {"q": "test"}, "openrouter", "model-x")
    key2 = compute_effect_key("llm", "planner", {"q": "test"}, "openrouter", "model-x")
    key3 = compute_effect_key("llm", "planner", {"q": "different"}, "openrouter", "model-x")
    assert key1 == key2
    assert key1 != key3


def test_replay_mode_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("SCOUT_MODE", "replay")
    from scout.config import load_settings
    load_settings()

    recorder = EffectRecorder(snapshot_dir=tmp_path)
    with pytest.raises(ReplayMissError):
        recorder.execute_or_replay("nonexistent-key", lambda: "some result")


def test_fault_injection_disabled_by_default():
    reset_fault_counters()
    # Should not raise or exit when faults disabled
    maybe_crash("after_intent_before_dispatch")
