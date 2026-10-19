"""Run Exporter for State, Events, and Trace (PRD FR-4.5).

Persists completed run artifacts to disk as JSONL files:
`data/runs/{run_id}.jsonl`
Includes an SHA256 integrity hash and allows safe TTL expiry of Redis keys.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import redis

from scout.store.redis_keys import (
    key_run_budget,
    key_run_events,
    key_run_findings,
    key_run_meta,
    key_run_report,
    key_run_trace,
)

logger = logging.getLogger(__name__)


class RunExporter:
    def __init__(self, r: redis.Redis, base_dir: str = "data/runs"):
        self.r = r
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def export_run(self, run_id: str) -> Path:
        """Exports all Redis data for run_id into a single structured JSONL file.

        Lines in the JSONL:
        1. {"type": "meta", "data": {...}}
        2. {"type": "budget", "data": {...}}
        3. {"type": "findings", "data": [...]}
        4. {"type": "report", "data": {...}}
        5. {"type": "events", "data": [...]}
        6. {"type": "trace", "data": [...]}
        7. {"type": "integrity", "sha256": "..."}
        """
        export_file = self.base_dir / f"{run_id}.jsonl"

        # 1. Read Meta
        meta_raw = self.r.hgetall(key_run_meta(run_id))
        meta = {k.decode("utf-8"): v.decode("utf-8") for k, v in meta_raw.items()}

        # 2. Read Budget
        budget_raw = self.r.hgetall(key_run_budget(run_id))
        budget = {k.decode("utf-8"): v.decode("utf-8") for k, v in budget_raw.items()}

        # 3. Read Findings
        findings_raw = self.r.hgetall(key_run_findings(run_id))
        findings = [
            json.loads(v.decode("utf-8")) for v in findings_raw.values()
        ]

        # 4. Read Report
        report_raw = self.r.get(key_run_report(run_id))
        report = json.loads(report_raw.decode("utf-8")) if report_raw else None

        # 5. Read Events Stream
        events_entries = self.r.xrange(key_run_events(run_id))
        events: List[Dict[str, Any]] = []
        for entry_id, fields in events_entries:
            eid = entry_id.decode("utf-8")
            decoded_fields = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
            events.append({"id": eid, **decoded_fields})

        # 6. Read Trace Stream
        trace_entries = self.r.xrange(key_run_trace(run_id))
        trace: List[Dict[str, Any]] = []
        for entry_id, fields in trace_entries:
            eid = entry_id.decode("utf-8")
            decoded_fields = {k.decode("utf-8"): v.decode("utf-8") for k, v in fields.items()}
            trace.append({"id": eid, **decoded_fields})

        records = [
            {"type": "meta", "data": meta},
            {"type": "budget", "data": budget},
            {"type": "findings", "data": findings},
            {"type": "report", "data": report},
            {"type": "events", "data": events},
            {"type": "trace", "data": trace},
        ]

        # Calculate payload integrity hash
        serialized_lines = [json.dumps(r, sort_keys=True) for r in records]
        content_hash = hashlib.sha256("\n".join(serialized_lines).encode("utf-8")).hexdigest()

        records.append({"type": "integrity", "sha256": content_hash})

        with open(export_file, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec) + "\n")

        logger.info("Exported run '%s' to %s (hash: %s)", run_id, export_file, content_hash)
        return export_file
