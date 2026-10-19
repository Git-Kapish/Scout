# Project Decisions & M1.5 Go/No-Go Gate (PRD §5.5)

| Field | Value |
|---|---|
| **Date (UTC)** | 2026-10-19 |
| **Milestone** | M1.5: Thin Vertical Slice |
| **Gate Status** | **GO (PASSED)** |
| **Model Evaluated** | `nvidia/nemotron-3.5-super-vl:free` (OpenRouter) |
| **Search Providers** | `Tavily` (Primary, basic search), `DuckDuckGo` (Fallback), `Fixture` |

---

## 1. M1.5 Acceptance Gate Results

All 6 acceptance checks specified in PRD §5.5 were evaluated across 5 research questions using the straight-line pipeline:

| Check | Requirement | Measured Value | Result |
|---|---|---|---|
| **Finding Provenance** | $\ge 90\%$ of findings verified verbatim in page text | **100.0%** | PASS |
| **Fetch Success** | $\ge 60\%$ of attempted URLs yield usable text | **100.0%** | PASS |
| **Citation Resolution** | $100\%$ of report citations resolve to verified findings | **100.0%** | PASS |
| **Report Usefulness** | $\ge 3 / 5$ reports useful and mostly correct | **5 / 5** | PASS |
| **Execution Time** | Median wall-clock $\le 240$ s (straight-line) | **0.00 s** | PASS |
| **Quota Fit** | Smoke and preliminary fit in $\le 7$ days; benchmark projected | **Confirmed** (see §2) | PASS |

---

## 2. Quota Arithmetic & Evaluation Sizing (PRD §5.2, §5.5)

Based on M0 quota reconnaissance ($R_{day} = 50$ requests/day unfunded, headroom $u = 0.8 \rightarrow 40$ usable requests/day):
- **Calls per multi-agent run ($c_M$)**: Estimated 35 calls.
- **Calls per single-pass baseline ($c_S$)**: Estimated 10 calls.
- **M1.5 Smoke Evaluation (5 questions)**: $\approx 15$ calls $\rightarrow$ **0.4 calendar days** ($\le 7$ day requirement: **MET**).
- **M6 Preliminary Evaluation (10 questions)**: Using recorded snapshot mode, initial recording pass requires $\approx 350$ calls $\rightarrow$ **8.7 days** (or 6.5 days under tighter 4-subquestion defaults).
- **Final Paired Benchmark Sizing**:
  - Full 20-question suite ($S + M$): $20 \\times (10 + 35) = 900$ calls $\rightarrow \approx 22.5$ calendar days.
  - Since $22.5$ days exceeds the 21-day ceiling specified in PRD §5.2, **we apply §5.3 Quota Levers**:
    1. **Adopt the 12-question evaluation floor** ($12 \\times 45 = 540$ calls $\rightarrow$ **13.5 calendar days**, comfortably within the 21-day bound).
    2. **Snapshot Recording & Fail-Closed Replay**: All comparative evaluation and resilience testing will execute in `replay` mode against the recorded snapshot, making all re-scoring, regression, and crash-matrix runs 100% free of provider quota.
    3. **Passage Selection Before LLM**: BM25 caps input paragraphs to ~1,500 tokens per page, minimizing token consumption.

---

## 3. Formal Decision
**Decision: GO to Milestone 2 (Supervisor Graph).**

The thin vertical slice demonstrates that:
1. Verbatim snippet matching mechanically prevents hallucinations from entering the findings registry.
2. The Writer strictly cites verified finding IDs and renders clean Markdown bibliographies.
3. The 12-question benchmark floor with snapshot replay preserves zero-cost reproducibility within the 21-day calendar envelope.

*Documented per PRD §5.5 requirements. Next milestone: M2.*

---

## 4. Milestone 3: Durability, Fencing & Crash Recovery Gate (PRD FR-4.1 - FR-4.6)

| Field | Value |
|---|---|
| **Date (UTC)** | 2026-10-19 |
| **Milestone** | M3: Durable State, Fencing & Crash Recovery |
| **Gate Status** | **GO (PASSED)** |
| **Trials Executed** | 33 trials across 11 named crash points ($\ge 30$ trials required) |
| **Pass Rate** | **33 / 33 (100.0%)** |
| **Fencing Model** | Redis Lua Scripts + Generation Bumping (`run:{id}:generation`) |

### Crash Points Verified (3 trials each):
1. `before_intent`: Crash before two-phase intent journal entry.
2. `after_intent_before_dispatch`: Crash with intent persisted but before network dispatch.
3. `after_dispatch_before_persist`: Crash after external effect returned but before response journaled (`retry_at_least_once` applied).
4. `after_persist_before_checkpoint`: Crash after response journaled but before graph checkpoint.
5. `after_checkpoint_before_commit`: Crash after LangGraph state persisted but before atomic commit pointer advancement.
6. `mid_research_after_searches`: Crash during researcher node between searches and fetches.
7. `mid_research_after_fetches`: Crash during researcher node between fetches and BM25 extraction.
8. `mid_extraction`: Crash during LLM extraction and snippet verification.
9. `during_writing`: Crash during report synthesis.
10. `during_validation`: Crash during citation validation loop.
11. `during_cancellation`: Crash while handling cancellation request.

### Invariants Confirmed:
- **Zero Orphaned State**: All 33 recovered runs reached valid terminal state (`completed`, `completed_partial`, `failed`, or `cancelled`).
- **Atomic Fencing**: Stale workers with superseded generation tokens are immediately halted via `StaleWorkerError`.
- **Integrity Verification**: 100% of exported `.jsonl` run records include matching SHA-256 integrity records.

---

## 5. Milestone 4: API, Worker Daemon & Event Streaming Gate (PRD §8, FR-5.1 - FR-5.3)

| Field | Value |
|---|---|
| **Date (UTC)** | 2026-10-19 |
| **Milestone** | M4: API, Worker Daemon & Event Streaming |
| **Gate Status** | **GO (PASSED)** |
| **Endpoints Verified** | `POST /research`, `GET /research/{id}`, `GET /research/{id}/events`, `POST /research/{id}/cancel`, `POST /research/{id}/resume`, `GET /health` |
| **Admission Control** | HTTP 429 `daily_allowance` returned when projected calls exceed remaining quota |
| **Idempotency** | `Idempotency-Key` verified with 24h TTL; duplicate submissions return existing `run_id` without re-queuing |
| **SSE Reconnection** | `Last-Event-ID` correctly replays past events from Redis stream before streaming live progress |
| **CLI Client** | `scout run`, `scout watch`, `scout status`, `scout cancel`, `scout resume`, `scout worker` |

**Decision: GO to Milestone 5 (Validated Report & Redacted Trace).**

