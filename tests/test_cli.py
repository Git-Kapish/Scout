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
