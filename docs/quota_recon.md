# Quota Reconnaissance Report (PRD §5.6)

| Field | Value |
|---|---|
| **Date (UTC)** | 2026-10-19 09:09:59 UTC |
| **Primary Provider** | OpenRouter (`:free` tier) |
| **Configured Model** | `nvidia/nemotron-3.5-super-vl:free` |
| **Evidence Level** | `documented` |
| **Documented RPM** | 20 req/min |
| **Documented Daily Allowance (R_day)** | 50 req/day |
| **Measured Conservative Allowance** | 50 req/day |
| **Headroom Factor (u)** | 0.8 |
| **Default Per-Run LLM Cap** | `min(60, floor(0.8 * 50)) = 40` calls |

## Probe Execution Summary
- **Probes Attempted**: 0
- **Probes Succeeded**: 0
- **Average Latency**: 0.00 s
- **Status Note**: OPENROUTER_API_KEY not found in environment or .env. Reconnaissance recorded documented baseline only. To measure live headers, set OPENROUTER_API_KEY in .env and re-run.

## Observed Rate-Limit Headers
```json
{}
```

## Error Diagnostics
None (all probes succeeded or mock mode).

## Feasibility & Planning Arithmetic (§5.2)
At `R_day = 50`:
- **Headroom allowance**: `0.8 * 50 = 40` requests/day.
- **Estimated calls per multi-agent run (c_M)**: ~35 calls.
- **Runs per day**: `floor(40 / 35) = 1` full multi-agent run/day on primary OpenRouter alone.
- **M1.5 Thin Slice Fit**: 5 smoke questions (~15 calls) fits within 1 day.
- **Decision Rule**: Follow §5.3 levers:
  1. Leverage `record/replay` mode for evaluation runs to eliminate repeated remote calls.
  2. Reserve OpenRouter for Planner, Reviewer, and Writer roles.
  3. Search provider Tavily uses basic search only (1 credit/search, 1000/mo free).

---
*Generated automatically by `scripts/quota_recon.py` adhering to PRD §5.6.*
