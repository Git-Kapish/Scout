"""Unit tests for the Scout Command-Line Interface (scout.cli)."""

from __future__ import annotations

import argparse
from unittest.mock import MagicMock, patch

import pytest

from scout.cli import cmd_ask, display_report_result, main


def test_display_report_result_splits_answer_and_citations(capsys):
    report_dict = {
        "title": "Redis vs SQLite",
        "summary": "Redis is in-memory whereas SQLite is embedded.",
        "markdown": (
            "# Redis vs SQLite\n\n"
            "## Executive Summary\n"
            "Redis operates entirely in memory.\n\n"
            "## References\n"
            "[1] Redis Docs - https://redis.io Quote: \"In-memory data store\"\n"
        ),
        "claims": [
            {"claim_text": "Redis is in-memory", "citations": ["[1]"], "finding_ids": ["f1"]}
        ],
        "sources": [{"label": "[1]", "url": "https://redis.io", "title": "Redis Docs"}],
    }
    budget = {"llm_calls": 5, "tokens_used": 1200, "searches": 2, "elapsed_s": 12.5}

    # Should run without error and format panels
    display_report_result(report_dict, budget=budget, question="Compare Redis and SQLite")


def test_cmd_ask_single_shot():
    with patch("scout.cli._execute_and_display_query") as mock_exec:
        args = argparse.Namespace(question="What is the speed of light?", api_url="http://127.0.0.1:8000")
        cmd_ask(args)
        mock_exec.assert_called_once_with("What is the speed of light?", api_url="http://127.0.0.1:8000")


def test_cmd_ask_interactive_exit():
    with patch("rich.prompt.Prompt.ask", side_effect=["exit"]):
        args = argparse.Namespace(question=None, api_url="http://127.0.0.1:8000")
        cmd_ask(args)


def test_cmd_ask_interactive_query_flow():
    with patch("rich.prompt.Prompt.ask", side_effect=["What is quantum computing?", "quit"]), \
         patch("scout.cli._execute_and_display_query") as mock_exec:
        args = argparse.Namespace(question=None, api_url="http://127.0.0.1:8000")
        cmd_ask(args)
        mock_exec.assert_called_once_with("What is quantum computing?", api_url="http://127.0.0.1:8000")


def test_cli_subparsers_registration():
    with patch("sys.argv", ["scout", "--help"]):
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0


def test_direct_local_reads_budget_hash(monkeypatch):
    """The direct-local fallback must read run:{id}:budget as a hash, not GET it (WRONGTYPE)."""
    import redis

    import scout.cli as cli
    from scout.graph.state import ResearchState
    from scout.store.redis_keys import key_run_budget

    r = redis.Redis(host="127.0.0.1", port=6379, db=0)
    captured = {}

    class FakeWorker:
        def __init__(self, **kwargs):
            pass

        def execute_run(self, run_id, initial_question):
            r.hset(key_run_budget(run_id), mapping={"llm_calls": "3", "tokens_in": "10", "tokens_out": "5"})
            return ResearchState(run_id=run_id, question=initial_question)

    monkeypatch.setattr("scout.worker.ResearchWorker", FakeWorker)
    monkeypatch.setattr(
        cli, "display_report_result", lambda report, budget, question, run_id: captured.update(budget=budget, run_id=run_id)
    )

    cli._execute_direct_local("Q?")

    assert captured["budget"]["llm_calls"] == 3
    for k in r.keys(f"*{captured['run_id']}*"):
        r.delete(k)


REPORT_MD = (
    "# Redis vs SQLite\n\n"
    "**Executive Summary:** Redis is in-memory.\n\n"
    "## Storage\n\n"
    "**Key Supported Claims:**\n"
    "- Redis keeps data in memory. [1]\n\n"
    "## References\n\n"
    "[1] **Redis Docs** - [https://redis.io/docs](https://redis.io/docs) (Retrieved 2026-10-20 10:00 UTC)\n"
    "> *Quote: \"Redis is an in-memory data store.\"*\n"
)


def test_parse_references_reads_rendered_sources():
    from scout.cli import _parse_references

    refs = _parse_references(REPORT_MD.split("## References", 1)[1])
    assert refs == [
        {
            "num": "1",
            "title": "Redis Docs",
            "url": "https://redis.io/docs",
            "retrieved": "2026-10-20 10:00 UTC",
            "quote": "Redis is an in-memory data store.",
        }
    ]


def test_report_shows_answer_before_sources(capsys, monkeypatch):
    monkeypatch.setenv("SCOUT_NO_STREAM", "1")
    display_report_result({"markdown": REPORT_MD, "claims": [{"citations": ["f1"]}]}, run_id="run_x")
    out = capsys.readouterr().out
    assert out.index("Redis keeps data in memory") < out.index("Sources (1)") < out.index("https://redis.io/docs")
    assert "Redis is an in-memory data store." in out
    assert "1/1 claims cited" in out


class _FakeStream:
    def __init__(self, lines):
        self.status_code = 200
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self._lines)


def _status_response(payload):
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    return resp


def test_watch_shows_report_when_run_completes(capsys, monkeypatch):
    """The answer is printed by the watch itself; no separate `scout status` call is needed."""
    monkeypatch.setenv("SCOUT_NO_STREAM", "1")
    lines = [
        "event: node_completed",
        'data: {"event": "node_completed", "node": "plan", "subquestions": "{\\"sq_1\\": \\"How does Redis store data?\\"}"}',
        "",
        "event: run_completed",
        'data: {"event": "run_completed", "reason": ""}',
        "",
    ]
    status = {"run_id": "r1", "status": "completed", "question": "Q", "budget": {"llm_calls": 3, "tokens_used": 100, "searches": 1},
              "report": {"markdown": REPORT_MD, "claims": []}}
    with patch("scout.cli.httpx.stream", return_value=_FakeStream(lines)), \
         patch("scout.cli.httpx.get", return_value=_status_response(status)):
        from scout.cli import cmd_watch

        cmd_watch(argparse.Namespace(run_id="r1", api_url="http://api"))
    out = capsys.readouterr().out
    assert "How does Redis store data?" in out
    assert "Redis keeps data in memory" in out
    assert "Sources (1)" in out


def test_watch_explains_failed_run(capsys):
    lines = ["event: run_failed", 'data: {"event": "run_failed", "reason": "provider_unavailable"}', ""]
    status = {"run_id": "r2", "status": "failed", "question": "Q", "report": None,
              "termination_reason": "provider_unavailable", "termination_detail": "404 model not found"}
    with patch("scout.cli.httpx.stream", return_value=_FakeStream(lines)), \
         patch("scout.cli.httpx.get", return_value=_status_response(status)):
        from scout.cli import cmd_watch

        cmd_watch(argparse.Namespace(run_id="r2", api_url="http://api"))
    out = capsys.readouterr().out
    assert "provider_unavailable" in out
    assert "404 model not found" in out
    assert "scout resume r2" in out
