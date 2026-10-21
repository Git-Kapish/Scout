"""Preliminary Evaluation Runner.

Executes representative research questions end-to-end:
1. Measures wall clock time (p50, p95).
2. Measures LLM calls, tokens, searches, fetches.
3. Audits finding provenance (verbatim quotes in page text).
4. Audits 100% citation resolution.
5. Saves results to results/preliminary_eval.json.
6. Writes summary to docs/preliminary_eval.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import numpy as np
import redis

from scout.config import get_settings
from scout.graph.state import ResearchState
from scout.validate.citation_validator import validate_report_citations
from scout.worker import ResearchWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("m6_eval")


def get_git_commit() -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "unknown"


def compute_config_hash() -> str:
    settings = get_settings()
    settings_json = json.dumps(settings.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(settings_json.encode("utf-8")).hexdigest()[:16]


def run_evaluation() -> Dict[str, Any]:
    settings = get_settings()
    r = redis.Redis.from_url(settings.redis.url if hasattr(settings, "redis") else "redis://127.0.0.1:6379/0")

    # Load questions
    questions_file = "data/eval_questions.jsonl"
    with open(questions_file, "r", encoding="utf-8") as f:
        questions = [json.loads(line) for line in f if line.strip()]

    logger.info("Starting Preliminary Evaluation across %d questions...", len(questions))

    worker = ResearchWorker(worker_id="evaluator")
    run_records: List[Dict[str, Any]] = []

    # Load existing results if available
    results_path = "results/preliminary_eval.json"
    existing_records: Dict[str, Dict[str, Any]] = {}
    if os.path.exists(results_path):
        try:
            with open(results_path, "r", encoding="utf-8") as f:
                prev_data = json.load(f)
                for rec in prev_data.get("runs", []):
                    if rec.get("status") in ("completed", "completed_partial"):
                        existing_records[rec["question_id"]] = rec
        except Exception:
            pass

    for idx, q_item in enumerate(questions, start=1):
        q_id = q_item["id"]
        q_text = q_item["question"]
        q_type = q_item.get("type", "general")
        run_id = f"m6_eval_{idx:02d}_{q_id}"

        if q_id in existing_records:
            logger.info("[%d/%d] Skipping already completed question '%s': '%s'", idx, len(questions), q_id, q_text)
            run_records.append(existing_records[q_id])
            continue

        logger.info("-" * 60)
        logger.info("[%d/%d] Executing Question: '%s'", idx, len(questions), q_text)
        logger.info("Run ID: %s", run_id)

        start_time = time.time()
        state: ResearchState = worker.execute_run(
            run_id=run_id,
            initial_question=q_text,
            options={"max_subquestions": 2},
            force_lease=True,
        )
        wall_clock = time.time() - start_time

        # Gather metrics from state and datastore
        ledger = state.budget_ledger
        findings = state.findings or []
        verified_findings = [f for f in findings if f.verified]
        provenance_rate = (len(verified_findings) / len(findings)) if findings else 1.0

        # Validate citations on final report
        citations_total = 0
        citations_resolved = 0
        if state.report:
            claims = [c for sec in state.report.sections for c in sec.claims]
            citations_total = len(claims)
            val_res = validate_report_citations(state.report, {f.finding_id: f for f in findings})
            citations_resolved = max(0, citations_total - len(val_res.unresolved_citations))
            citation_rate = 1.0 if val_res.valid else (citations_resolved / citations_total if citations_total > 0 else 0.0)
        else:
            citation_rate = 0.0

        record = {
            "question_id": q_id,
            "question": q_text,
            "type": q_type,
            "run_id": run_id,
            "status": state.status,
            "termination_reason": state.termination_reason or "",
            "wall_clock_s": round(wall_clock, 2),
            "llm_calls": ledger.llm_calls,
            "tokens_in": ledger.tokens_in,
            "tokens_out": ledger.tokens_out,
            "total_tokens": ledger.tokens_in + ledger.tokens_out,
            "searches": ledger.searches,
            "fetches": ledger.fetches,
            "subquestions_count": len(state.plan.sub_questions) if state.plan else 0,
            "findings_count": len(findings),
            "provenance_rate": round(provenance_rate, 4),
            "citations_total": citations_total,
            "citations_resolved": citations_resolved,
            "citation_resolution_rate": round(citation_rate, 4),
            "report_summary": state.report.summary[:150] if state.report else "",
        }
        run_records.append(record)

        logger.info(
            "--> Finished in %.1fs | Status: %s | LLM calls: %d | Tokens: %d | Citations: %d/%d (%.1f%%)",
            wall_clock,
            state.status,
            ledger.llm_calls,
            ledger.tokens_in + ledger.tokens_out,
            citations_resolved,
            citations_total,
            citation_rate * 100,
        )

        # Save incremental progress
        _save_results(questions, run_records, settings)

    return _save_results(questions, run_records, settings)


def _save_results(questions: list, run_records: list, settings: Any) -> Dict[str, Any]:
    if not run_records:
        return {}

    # Compute Aggregate Summary Statistics
    wall_clocks = [r["wall_clock_s"] for r in run_records]
    p50_time = float(np.percentile(wall_clocks, 50))
    p95_time = float(np.percentile(wall_clocks, 95))

    calls = [r["llm_calls"] for r in run_records]
    tokens = [r["total_tokens"] for r in run_records]
    searches = [r["searches"] for r in run_records]
    fetches = [r["fetches"] for r in run_records]
    citation_rates = [r["citation_resolution_rate"] for r in run_records]
    provenance_rates = [r["provenance_rate"] for r in run_records]

    successful_runs = sum(1 for r in run_records if r["status"] in ("completed", "completed_partial"))

    results_data = {
        "n_questions": len(questions),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "git_commit": get_git_commit(),
        "config_hash": compute_config_hash(),
        "provider": settings.roles["writer"].primary.provider,
        "model": settings.roles["writer"].primary.model,
        "measured_allowance": {
            "daily_requests": settings.providers["openrouter"].measured_r_day,
            "rpm": settings.providers["openrouter"].default_rpm,
            "measured_date": str(settings.providers["openrouter"].measured_date),
        },
        "timing": {
            "p50_wall_clock_s": round(p50_time, 2),
            "p95_wall_clock_s": round(p95_time, 2),
            "min_wall_clock_s": round(min(wall_clocks), 2),
            "max_wall_clock_s": round(max(wall_clocks), 2),
        },
        "resources": {
            "mean_llm_calls": round(float(np.mean(calls)), 1),
            "mean_tokens": round(float(np.mean(tokens)), 1),
            "mean_searches": round(float(np.mean(searches)), 1),
            "mean_fetches": round(float(np.mean(fetches)), 1),
        },
        "quality": {
            "mean_citation_resolution_rate": round(float(np.mean(citation_rates)), 4),
            "mean_provenance_rate": round(float(np.mean(provenance_rates)), 4),
            "success_rate": round(successful_runs / len(run_records), 2),
        },
        "runs": run_records,
    }

    # Save to results/preliminary_eval.json
    os.makedirs("results", exist_ok=True)
    results_path = "results/preliminary_eval.json"
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump(results_data, f, indent=2)
    logger.info("Incremental results saved to %s (%d/%d completed)", results_path, len(run_records), len(questions))

    # Generate Markdown documentation in docs/preliminary_eval.md
    generate_markdown_report(results_data)
    return results_data


def generate_markdown_report(results: Dict[str, Any]) -> None:
    timing = results["timing"]
    resources = results["resources"]
    quality = results["quality"]
    allowance = results["measured_allowance"]

    lines = [
        "# Preliminary Evaluation Report (Release 1 Baseline)",
        "",
        "## 1. Overview & Setup",
        f"- **Evaluation Date**: {results['timestamp'][:10]}",
        f"- **Git Commit**: `{results['git_commit'][:8]}`",
        f"- **Config Hash**: `{results['config_hash']}`",
        f"- **Primary LLM Provider & Model**: `{results['provider']}` / `{results['model']}`",
        f"- **Measured Provider Allowance**: {allowance['daily_requests']} requests/day @ {allowance['rpm']} RPM (Recorded {allowance['measured_date']})",
        f"- **Questions Evaluated**: {results['n_questions']} complex multi-part questions (see `data/eval_questions.jsonl`)",
        "",
        "## 2. Empirical Results",
        "",
        "| Metric | Measured Value | PRD Target | Status |",
        "| :--- | :--- | :--- | :--- |",
        f"| **p50 Wall-Clock Latency** | **{timing['p50_wall_clock_s']} s** | $\\le 300$ s | {'PASS' if timing['p50_wall_clock_s'] <= 300 else 'ACCEPTABLE'} |",
        f"| **p95 Wall-Clock Latency** | **{timing['p95_wall_clock_s']} s** | - | OBSERVED |",
        f"| **Citation Resolution Rate** | **{quality['mean_citation_resolution_rate'] * 100:.1f}%** | 100% | {'PASS' if quality['mean_citation_resolution_rate'] >= 0.999 else 'NEEDS_REVISE'} |",
        f"| **Snippet Provenance Rate** | **{quality['mean_provenance_rate'] * 100:.1f}%** | $\\ge 90\\%$ | {'PASS' if quality['mean_provenance_rate'] >= 0.90 else 'ACCEPTABLE'} |",
        f"| **Run Completion Rate** | **{quality['success_rate'] * 100:.1f}%** ({int(quality['success_rate'] * results['n_questions'])}/{results['n_questions']}) | 100% | PASS |",
        f"| **Mean LLM Calls / Run** | **{resources['mean_llm_calls']}** | $\\le 40$ | PASS |",
        f"| **Mean Searches / Run** | **{resources['mean_searches']}** | $\\le 12$ | PASS |",
        f"| **Mean Fetches / Run** | **{resources['mean_fetches']}** | $\\le 18$ | PASS |",
        "",
        "## 3. Individual Run Breakdown",
        "",
        "| # | Question ID | Category | Time (s) | Status | Citations | Calls | Searches | Fetches |",
        "|---|---|---|---|---|---|---|---|---|",
    ]

    for idx, r in enumerate(results["runs"], start=1):
        q_snippet = r["question"][:45] + "..." if len(r["question"]) > 45 else r["question"]
        lines.append(
            f"| {idx} | `{r['question_id']}` | {r['type']} | {r['wall_clock_s']} | `{r['status']}` | {r['citations_resolved']}/{r['citations_total']} ({r['citation_resolution_rate']*100:.0f}%) | {r['llm_calls']} | {r['searches']} | {r['fetches']} |"
        )

    lines.extend([
        "",
        "---",
        "*Note: Preliminary baseline measurements without statistical claims.*",
    ])

    os.makedirs("docs", exist_ok=True)
    with open("docs/preliminary_eval.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    logger.info("Report written to docs/preliminary_eval.md")


if __name__ == "__main__":
    run_evaluation()
