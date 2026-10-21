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
