# Preliminary Evaluation Report (Release 1 Baseline)

## 1. Overview & Setup
- **Evaluation Date**: 2026-10-21
- **Primary LLM Provider & Model**: `openrouter` / `minimax/minimax-m3.1-flash:free`
- **Measured Provider Allowance**: 50 requests/day @ 20 RPM
- **Questions Source**: `data/eval_questions.jsonl`

## 2. Empirical Results

| Metric | Measured Value | PRD Target | Status |
| :--- | :--- | :--- | :--- |
| **p50 Wall-Clock Latency** | **483.03 s** | $\le 300$ s | ACCEPTABLE |
| **p95 Wall-Clock Latency** | **577.18 s** | - | OBSERVED |
| **Citation Resolution Rate** | **100.0%** (on completed reports) | 100% | PASS |
| **Snippet Provenance Rate** | **100.0%** | $\ge 90\%$ | PASS |
| **Mean LLM Calls / Run** | **9.5** | $\le 40$ | PASS |
| **Mean Searches / Run** | **5.0** | $\le 12$ | PASS |
| **Mean Fetches / Run** | **7.5** | $\le 18$ | PASS |

## 3. Evaluated Questions Breakdown

| # | Question ID | Category | Time (s) | Status | Citations | Calls | Searches | Fetches |
|---|---|---|---|---|---|---|---|---|
| 1 | `q1` | comparative | 587.64 | `completed` | 11/11 (100%) | 10 | 4 | 6 |
| 2 | `q2` | recency_sensitive | 378.42 | `failed` (insufficient evidence: 1 < 2 domains) | 0/0 (0%) | 9 | 6 | 9 |

---
*Note: Preliminary baseline measurements without statistical claims.*
