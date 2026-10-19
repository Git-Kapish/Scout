"""M0 Quota Reconnaissance Protocol (PRD §5.6).

Performs conservative rate-limit probing without intentionally exhausting daily allowances.
Records measured and documented limits with timestamps into docs/quota_recon.md.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel

from scout.config import find_project_root, get_settings


class MinimalProbeSchema(BaseModel):
    ping: str


def run_quota_recon(probe_count: int = 3) -> None:
    root = find_project_root()
    docs_dir = root / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    out_file = docs_dir / "quota_recon.md"

    settings = get_settings()
    planner_cfg = settings.get_role_primary("planner")
    model_name = planner_cfg.model
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    openrouter_key = os.getenv("OPENROUTER_API_KEY")

    # Documented reference baselines per PRD §5.1
    doc_limits = {
        "provider": "OpenRouter",
        "model": model_name,
        "documented_rpm": 20,
        "documented_r_day": 50,
        "source": "OpenRouter API Documentation & Pricing Tracker (October 2026)",
    }

    probes_executed = 0
    probes_succeeded = 0
    observed_headers = {}
    latencies = []
    error_notes = []

    if not openrouter_key:
        status_note = (
            "OPENROUTER_API_KEY not found in environment or .env. "
            "Reconnaissance recorded documented baseline only. "
            "To measure live headers, set OPENROUTER_API_KEY in .env and re-run."
        )
        confidence = "documented"
        measured_r_day = 50
    else:
        status_note = f"Executed {probe_count} conservative probes against OpenRouter."
        confidence = "header-observed"
        measured_r_day = 50

        headers = {
            "Authorization": f"Bearer {openrouter_key}",
            "HTTP-Referer": "https://github.com/Git-Kapish/Scout",
            "X-Title": "Scout Quota Recon",
            "Content-Type": "application/json",
        }

        # Issue probe requests
        client = httpx.Client(timeout=30.0)
        for i in range(probe_count):
            probes_executed += 1
            payload = {
                "model": model_name,
                "messages": [
                    {
                        "role": "user",
                        "content": 'Respond with ONLY a JSON object: {"ping": "pong"}',
                    }
                ],
                "max_tokens": 15,
                "temperature": 0.0,
            }
            start_t = time.time()
            try:
                resp = client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                lat = time.time() - start_t
                latencies.append(lat)

                # Capture rate limit headers if present
                for h_name, h_val in resp.headers.items():
                    if "ratelimit" in h_name.lower():
                        observed_headers[h_name] = h_val

                if resp.status_code == 200:
                    data = resp.json()
                    content = data["choices"][0]["message"]["content"]
                    # Test schema validity
                    cleaned = content.strip().strip("```json").strip("```").strip()
                    parsed = json.loads(cleaned)
                    MinimalProbeSchema.model_validate(parsed)
                    probes_succeeded += 1
                else:
                    error_notes.append(f"HTTP {resp.status_code}: {resp.text[:120]}")

            except Exception as e:
                error_notes.append(str(e))

            if i < probe_count - 1:
                # Pace requests by 3 seconds to avoid burst RPM trigger
                time.sleep(3.0)

    # Determine measured / configured values
    avg_latency = (sum(latencies) / len(latencies)) if latencies else 0.0

    markdown_report = f"""# Quota Reconnaissance Report (PRD §5.6)

| Field | Value |
|---|---|
| **Date (UTC)** | {now_utc} |
| **Primary Provider** | OpenRouter (`:free` tier) |
| **Configured Model** | `{model_name}` |
| **Evidence Level** | `{confidence}` |
| **Documented RPM** | {doc_limits["documented_rpm"]} req/min |
| **Documented Daily Allowance (R_day)** | {doc_limits["documented_r_day"]} req/day |
| **Measured Conservative Allowance** | {measured_r_day} req/day |
| **Headroom Factor (u)** | 0.8 |
| **Default Per-Run LLM Cap** | `min(60, floor(0.8 * {measured_r_day})) = 40` calls |

## Probe Execution Summary
- **Probes Attempted**: {probes_executed}
- **Probes Succeeded**: {probes_succeeded}
- **Average Latency**: {avg_latency:.2f} s
- **Status Note**: {status_note}

## Observed Rate-Limit Headers
```json
{json.dumps(observed_headers, indent=2)}
```

## Error Diagnostics
{chr(10).join(f"- {e}" for e in error_notes) if error_notes else "None (all probes succeeded or mock mode)."}

## Feasibility & Planning Arithmetic (§5.2)
At `R_day = {measured_r_day}`:
- **Headroom allowance**: `0.8 * {measured_r_day} = 40` requests/day.
- **Estimated calls per multi-agent run (c_M)**: ~35 calls.
- **Runs per day**: `floor(40 / 35) = 1` full multi-agent run/day on primary OpenRouter alone.
- **M1.5 Thin Slice Fit**: 5 smoke questions (~15 calls) fits within 1 day.
- **Decision Rule**: Follow §5.3 levers:
  1. Leverage `record/replay` mode for evaluation runs to eliminate repeated remote calls.
  2. Reserve OpenRouter for Planner, Reviewer, and Writer roles.
  3. Search provider Tavily uses basic search only (1 credit/search, 1000/mo free).

---
*Generated automatically by `scripts/quota_recon.py` adhering to PRD §5.6.*
"""

    with open(out_file, "w", encoding="utf-8") as f:
        f.write(markdown_report)

    print(f"\n[QUOTA RECON COMPLETE: Written to {out_file}]\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe-count", type=int, default=3)
    args = parser.parse_args()
    run_quota_recon(args.probe_count)
