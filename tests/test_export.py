"""Tests for Run Exporter (PRD FR-4.5)."""

import json

import pytest
import redis

from scout.store.export import RunExporter
from scout.store.redis_keys import (
    key_run_budget,
    key_run_events,
    key_run_findings,
    key_run_meta,
    key_run_report,
    key_run_trace,
)


@pytest.fixture
def redis_client():
    r = redis.Redis(host="127.0.0.1", port=6379, db=4)
    r.flushdb()
    yield r
    r.flushdb()


def test_run_exporter_jsonl(redis_client, tmp_path):
    exporter = RunExporter(redis_client, base_dir=str(tmp_path))
    run_id = "run_export_test"

    # Populate mock run data
    redis_client.hset(key_run_meta(run_id), mapping={"status": "completed", "question": "What is AI?"})
    redis_client.hset(key_run_budget(run_id), mapping={"tokens_in": "1000", "tokens_out": "500", "llm_calls": "2"})
    redis_client.hset(
        key_run_findings(run_id),
        "f1",
        json.dumps({"finding_id": "f1", "claim": "AI is artificial intelligence", "verified": True}),
    )
    redis_client.set(key_run_report(run_id), json.dumps({"title": "AI Report", "markdown": "# AI"}))
    redis_client.xadd(key_run_events(run_id), {"event": "phase_started", "phase": "plan"})
    redis_client.xadd(key_run_trace(run_id), {"step": "plan", "tokens": "100"})

    # Export
    exported_file = exporter.export_run(run_id)
    assert exported_file.exists()

    with open(exported_file, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f]

    types = [item["type"] for item in lines]
    assert types == ["meta", "budget", "findings", "report", "events", "trace", "integrity"]

    # Check integrity sha256 exists
    integrity_rec = lines[-1]
    assert integrity_rec["type"] == "integrity"
    assert len(integrity_rec["sha256"]) == 64
