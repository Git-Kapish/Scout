# Scout CLI & API Developer Guide

This guide covers running and interacting with Scout via the command-line interface (CLI) and REST API.

---

## 1. Command-Line Interface (CLI)

The Scout CLI provides both an interactive research shell and scriptable commands for automation and observability.

### 1.1 Interactive Research Shell
Run Scout without arguments (or with `ask` / `chat`) to launch an interactive research session:
```bash
uv run scout
# or
uv run scout ask
# or
uv run scout chat
```

```text
┌───────────────────────────────── Scout CLI ─────────────────────────────────┐
│                                                                             │
│  🔭 Scout Interactive Research Assistant                                    │
│  Ask any research question. Scout executes multi-agent planning, web        │
│  search, evidence extraction, and synthesis to provide a real answer,       │
│  followed by verified citations and validation integrity checks.            │
│                                                                             │
│  Commands: Type your query and hit Enter. Type 'exit' or 'quit' to return.  │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘

Scout [Ask]: What are the performance trade-offs between Redis and SQLite?
```

- Type your research query and press **Enter**.
- Scout streams live planning and search progress to your terminal.
- Once finished, Scout displays:
  1. **💡 RESEARCH ANSWER**: Structured executive summary and narrative findings.
  2. **📚 VERIFIED CITATIONS & EVIDENCE SOURCES**: Source URLs, titles, and exact quotes.
  3. **🛡️ Quality & Integrity Audit**: Citation resolution rate, schema validation, and token/time statistics.
- Type `exit` or `quit` to cleanly exit the shell.

---

### 1.2 Single-Shot Query Execution
Submit a query directly from the command line:
```bash
uv run scout ask "What changed in the EU AI Act's general-purpose model obligations?"
```

To submit and detach without streaming live output:
```bash
uv run scout run "Compare Redis and SQLite" --no-watch
```

---

### 1.3 Monitoring, Tracing, and Lifecycle Management

| Command | Description | Example |
|---|---|---|
| `scout watch <run_id>` | Stream real-time Server-Sent Events from an active research run | `uv run scout watch run_da89cd251f4c` |
| `scout status <run_id>` | Inspect run status, progress, budget counters, and rendered report | `uv run scout status run_da89cd251f4c` |
| `scout trace <run_id>` | View ordered execution steps, tool latencies, and agent actions | `uv run scout trace run_da89cd251f4c` |
| `scout cancel <run_id>` | Request cooperative cancellation of an active run | `uv run scout cancel run_da89cd251f4c` |
| `scout resume <run_id>` | Recover and resume execution of a stalled or crashed run | `uv run scout resume run_da89cd251f4c` |
| `scout runs list` | Inspect all active and historical runs registered in Redis | `uv run scout runs list` |
| `scout runs purge` | Purge stalled or unleased runs from the active runs registry | `uv run scout runs purge` |
| `scout worker` | Start the autonomous background research worker daemon | `uv run scout worker` |

---

## 2. REST API Reference

The Scout FastAPI service runs on port `8000` by default.

### 2.1 Endpoints

#### Submit Research Run
- **`POST /research`**
  - **Headers**: `Idempotency-Key` *(optional UUID)*
  - **Request Body**:
    ```json
    {
      "question": "Compare the latency and throughput of Groq LPUs versus Nvidia H100 GPUs",
      "options": {}
    }
    ```
  - **Responses**:
    - `202 Accepted`: Returns `{"run_id": "run_...", "status": "queued"}`.
    - `429 Too Many Requests`: Triggered when projected usage exceeds daily quota allowance. Returns `{"code": "DAILY_QUOTA_EXHAUSTED", "retry_after_s": 3600}`.

#### Stream Real-Time Events
- **`GET /research/{run_id}/events`**
  - **Protocol**: Server-Sent Events (SSE)
  - **Headers**: `Last-Event-ID` *(optional, for resuming disconnected streams)*
  - **Event Types**:
    - `run_started`: Run metadata and question confirmation.
    - `plan_created`: Generated sub-questions.
    - `subquestion_started`: Sub-question being investigated.
    - `search_completed`: Search query completed.
    - `page_fetched`: Web page successfully downloaded.
    - `findings_extracted`: Verbatim verified findings count.
    - `review_decision`: Supervisor quality decision (`accept` / `rework`).
    - `budget_warning`: Resource budget advisory.
    - `writing_started`: Synthesis phase begun.
    - `validation_result`: Citation integrity audit outcome.
    - `run_completed`: Final completion notification.

#### Get Run Status and Report
- **`GET /research/{run_id}`**
  - Returns current status, budget counters, subquestion progress, and the final synthesized report:
    ```json
    {
      "run_id": "run_da89cd251f4c",
      "status": "completed",
      "question": "Compare Redis and SQLite",
      "progress": {
        "subquestions_done": 2,
        "subquestions_total": 2
      },
      "budget": {
        "llm_calls": 10,
        "tokens_used": 14200,
        "searches": 4,
        "elapsed_s": 483.0
      },
      "report": {
        "title": "Redis vs. SQLite Performance Analysis",
        "summary": "...",
        "markdown": "# Redis vs. SQLite...",
        "claims": [...],
        "sources": [...]
      }
    }
    ```

#### Cancel Active Run
- **`POST /research/{run_id}/cancel`**
  - Cooperatively stops execution at the next safe boundary.

#### Resume Stalled Run
- **`POST /research/{run_id}/resume`**
  - Reclaims an abandoned run using monotonic fencing and resumes from the last committed LangGraph checkpoint.

#### Ordered Execution Trace
- **`GET /research/{run_id}/trace?include=payloads`**
  - Returns the chronological sequence of execution steps, including step latencies, agent node names, tool calls, and payload summaries.

#### Health Check
- **`GET /health`**
  - Returns Redis connectivity, OpenRouter provider readiness, and search provider status.

---

## 3. Configuration & Environment Variables

Key parameters in `config/settings.yaml`:

| Section | Parameter | Default | Purpose |
|---|---|---|---|
| `roles.*.primary` | `model` | `minimax/minimax-m3.1-flash:free` | Configured OpenRouter model for each agent role |
| `budgets.default` | `total_llm_calls` | `40` | Hard cap on LLM invocations per run |
| `budgets.default` | `wall_clock_s` | `1200` | Hard timeout (seconds) per research run |
| `search` | `provider_order` | `["tavily", "ddgs"]` | Fallback search provider precedence |
| `redis` | `lease_ttl_s` | `15` | Worker lease TTL for crash detection |
| `admission_control` | `conservative_reserve_per_run` | `35` | Projected LLM calls required to accept a new run |

Required environment variables in `.env`:
```bash
OPENROUTER_API_KEY=your_openrouter_api_key
TAVILY_API_KEY=your_tavily_api_key
REDIS_URL=redis://127.0.0.1:6379/0
```
