"""Scout Command-Line Interface (PRD §8, §13).

Provides commands:
- scout (or scout ask): Interactive research assistant shell for user queries.
- scout ask "<question>": Submits query and renders real answer + verified citations.
- scout run "<question>": Submits a new research run and watches events live.
- scout watch <run_id>: Streams real-time Server-Sent Events from an active run.
- scout status <run_id>: Retrieves current run status, progress, budget, and report.
- scout cancel <run_id>: Requests cooperative cancellation.
- scout resume <run_id>: Recovers/resumes a stalled run.
- scout worker: Runs the autonomous background worker daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import threading
import time
import uuid
from typing import Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.rule import Rule
from rich.table import Table

console = Console()
DEFAULT_API_URL = os.getenv("SCOUT_API_URL", "http://127.0.0.1:8000")


def cmd_run(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    question = args.question.strip()
    if not question:
        console.print("[bold red]Error:[/] Question cannot be empty.")
        sys.exit(1)

    console.print(Panel(f"[bold cyan]Submitting Research Question:[/]\n{question}", title="Scout"))
    try:
        resp = httpx.post(
            f"{api_url}/research",
            json={"question": question},
            headers={"Idempotency-Key": args.idempotency_key} if args.idempotency_key else {},
            timeout=10.0,
        )
    except Exception as e:
        console.print(f"[bold red]Connection Error:[/] Could not connect to API at {api_url}: {e}")
        sys.exit(1)

    if resp.status_code == 429:
        err = resp.json()
        console.print(
            f"[bold yellow]Admission Control Rejected (HTTP 429):[/] {err.get('code')}\n"
            f"Projected calls: {err.get('projected_calls')} > Remaining calls: {err.get('remaining_calls')}\n"
            f"Retry after: {err.get('retry_after_s')} seconds."
        )
        sys.exit(2)

    if resp.status_code != 202:
        console.print(f"[bold red]API Error ({resp.status_code}):[/] {resp.text}")
        sys.exit(1)

    data = resp.json()
    run_id = data["run_id"]
    console.print(f"[bold green]Accepted Run ID:[/] {run_id} (status: [cyan]{data['status']}[/])")

    if not args.no_watch:
        cmd_watch(argparse.Namespace(run_id=run_id, api_url=api_url))


TERMINAL_EVENTS = ("run_completed", "run_completed_partial", "run_failed", "run_cancelled")
TERMINAL_STATUSES = ("completed", "completed_partial", "failed", "cancelled")

# Next activity after each graph node finishes (node_completed events carry the finished node).
_NEXT_STAGE = {
    "init_run": "Planning sub-questions",
    "plan": "Choosing the first sub-question",
    "research": "Reviewing the evidence",
    "review": "Choosing the next step",
    "write": "Validating citations",
    "validate_citations": "Finalizing the report",
}

# Actionable hints for failure reasons recorded in run meta.
_FAILURE_HINTS = {
    "quota_limited": "The model provider's free quota is exhausted. Wait for it to reset, then run `scout resume {run_id}`.",
    "provider_unavailable": "The model provider rejected the request. Check OPENROUTER_API_KEY and that the model in config/settings.yaml is still offered, then `scout resume {run_id}`.",
    "replay_miss": "Replay mode had no recorded response for a call. Re-record the fixture or set scout_mode to live.",
    "insufficient_evidence": "Not enough verified evidence was found to write a grounded answer. Try a narrower or rephrased question.",
    "missing_report": "The writer did not produce a report.",
    "wall_clock_timeout": "The run hit its wall-clock budget.",
    "llm_calls_exhausted": "The run used up its LLM call budget.",
    "token_budget_exhausted": "The run used up its token budget.",
    "max_resume_attempts_exceeded": "The run crashed too many times and was given up.",
}


def _stream_enabled() -> bool:
    return console.is_terminal and not os.getenv("SCOUT_NO_STREAM")


class _ProgressView:
    """Turns SSE events into a live spinner line plus persistent milestone lines."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.started = time.monotonic()
        self.stage = "Waiting for a worker"
        self.subquestions: dict[str, str] = {}
        self.active_sq: Optional[str] = None
        self.status = console.status(self._status_text(), spinner="dots")

    def _status_text(self) -> str:
        return f"[cyan]{self.stage}…[/] [dim]({time.monotonic() - self.started:.0f}s · Ctrl+C to detach)[/]"

    def __enter__(self) -> "_ProgressView":
        self.status.start()
        return self

    def __exit__(self, *exc) -> None:
        self.status.stop()

    def _set_stage(self, stage: str) -> None:
        self.stage = stage
        self.tick()

    def tick(self) -> None:
        self.status.update(self._status_text())

    def handle(self, event_type: str, payload: dict) -> None:
        if event_type == "run_started":
            self._set_stage("Waiting for a worker")
        elif event_type == "node_completed":
            self._node_completed(payload)
        elif event_type == "budget_warning":
            console.print("  [orange3]⚠ 80% of the run budget used[/]")
        elif event_type == "budget_exhausted":
            console.print(f"  [orange3]⚠ Budget exhausted ({payload.get('reason')}); writing with the evidence so far[/]")
        elif event_type == "run_cancel_requested":
            self._set_stage("Cancelling")
        elif event_type == "run_resumed":
            self._set_stage("Resuming from the last checkpoint")
        else:
            self.tick()

    def _node_completed(self, payload: dict) -> None:
        node = payload.get("node", "")
        if node == "plan" and payload.get("subquestions"):
            try:
                self.subquestions = dict(json.loads(payload["subquestions"]))
            except (TypeError, ValueError):
                self.subquestions = {}
            console.print(f"[bold]●[/] Planned {len(self.subquestions)} sub-questions")
            for idx, q in enumerate(self.subquestions.values(), 1):
                console.print(f"  [dim]{idx}.[/] {escape(q)}")
        elif node == "pick_next":
            sq_id = payload.get("subquestion_id")
            if sq_id:
                self.active_sq = sq_id
                question = self.subquestions.get(sq_id, sq_id)
                self._set_stage(f"Researching: {escape(question)}")
                return
            self._set_stage("Writing the answer")
            return
        elif node == "research" and "findings" in payload:
            console.print(f"  [dim]↳ {payload['findings']} verified findings for {self.active_sq or 'sub-question'}[/]")
        elif node == "review" and "accepted" in payload:
            console.print(
                f"[bold]●[/] Sub-questions: [green]{payload['accepted']} accepted[/]"
                + (f", [yellow]{payload['abandoned']} abandoned[/]" if payload.get("abandoned", "0") != "0" else "")
            )
        self._set_stage(_NEXT_STAGE.get(node, self.stage))


def _parse_sse(response: httpx.Response):
    """Yields (event_type, event_id, raw_data) from an SSE response."""
    event_type, event_id, data_buffer = "progress", None, []
    for line in response.iter_lines():
        line = line.strip()
        if not line:
            if data_buffer:
                yield event_type, event_id, "\n".join(data_buffer)
            event_type, event_id, data_buffer = "progress", None, []
            continue
        if line.startswith("id:"):
            event_id = line[3:].strip()
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_buffer.append(line[5:].strip())


def cmd_watch(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    run_id = args.run_id
    last_event_id: Optional[str] = getattr(args, "last_event_id", None)
    max_reconnects = 10
    reconnect_delay = 2.0
    events_url = f"{api_url}/research/{run_id}/events"

    with _ProgressView(run_id) as view:
        for attempt in range(max_reconnects + 1):
            headers = {"Last-Event-ID": last_event_id} if last_event_id else {}
            params = {"last_event_id": last_event_id} if last_event_id else {}
            try:
                with httpx.stream("GET", events_url, params=params, headers=headers, timeout=None) as response:
                    if response.status_code == 404:
                        view.status.stop()
                        console.print(f"[bold red]Error:[/] Run '{run_id}' not found.")
                        return
                    for event_type, event_id, raw_data in _parse_sse(response):
                        if event_id:
                            last_event_id = event_id
                        try:
                            payload = json.loads(raw_data)
                        except ValueError:
                            payload = {"raw": raw_data}
                        if event_type in TERMINAL_EVENTS:
                            break
                        view.handle(event_type, payload)
                # Stream ended: done if the run is terminal, otherwise the connection dropped early
                if _run_status(run_id, api_url) in TERMINAL_STATUSES:
                    break
                raise httpx.RemoteProtocolError("event stream closed before the run finished")
            except KeyboardInterrupt:
                view.status.stop()
                console.print(f"\n[yellow]Detached.[/] The run continues in the background: [bold]scout watch {run_id}[/]")
                return
            except httpx.HTTPError as e:
                if _run_status(run_id, api_url) in TERMINAL_STATUSES:
                    break
                if attempt >= max_reconnects:
                    view.status.stop()
                    console.print(
                        f"\n[dim]Disconnected from stream ({e}). Reconnect with: scout watch {run_id}[/]"
                    )
                    return
                view.stage = "Reconnecting"
                view.tick()
                time.sleep(reconnect_delay)

    _fetch_and_display_report(run_id, api_url)


def _run_status(run_id: str, api_url: str) -> Optional[str]:
    try:
        return httpx.get(f"{api_url}/research/{run_id}", timeout=5.0).json().get("status")
    except Exception:
        return None


_REF_LINE = re.compile(r"^\[(\d+)\]\s+\*\*(.+?)\*\*\s+-\s+\[(.+?)\]\((.+?)\)(?:\s+\(Retrieved (.+?)\))?\s*$")
_QUOTE_LINE = re.compile(r"^(?:>\s*)?\*Quote:\s*\"(.*)\"\*\s*$")


def _parse_references(references_md: str) -> list[dict]:
    """Parses the renderer's References section into [{num, title, url, retrieved, quote}]."""
    refs: list[dict] = []
    for raw in references_md.splitlines():
        line = raw.strip()
        m = _REF_LINE.match(line)
        if m:
            refs.append({"num": m.group(1), "title": m.group(2), "url": m.group(4), "retrieved": m.group(5), "quote": None})
            continue
        q = _QUOTE_LINE.match(line)
        if q and refs:
            refs[-1]["quote"] = q.group(1)
    return refs


def _print_markdown(md: str) -> None:
    """Prints Markdown, streaming it block by block and word by word on an interactive terminal."""
    if not _stream_enabled():
        console.print(Markdown(md))
        return
    from rich.live import Live

    blocks = [b for b in re.split(r"\n\s*\n", md) if b.strip()]
    total_words = max(1, len(md.split()))
    delay = min(0.015, 4.0 / total_words)  # cap the whole answer at ~4 s
    for block in blocks:
        words = re.split(r"(\s+)", block)
        shown = ""
        with Live(Markdown(""), console=console, refresh_per_second=30, transient=False) as live:
            for idx, piece in enumerate(words):
                shown += piece
                if piece.strip() and idx % 2 == 0:
                    live.update(Markdown(shown))
                    time.sleep(delay)
            live.update(Markdown(block))


def display_report_result(
    report_dict: dict,
    budget: Optional[dict] = None,
    question: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    """Renders the answer first, then a numbered sources list, then a one-line run summary."""
    markdown_text = report_dict.get("markdown", "")
    if "## References" in markdown_text:
        answer_md, references_md = markdown_text.split("## References", 1)
    else:
        answer_md, references_md = markdown_text, ""
    answer_md = answer_md.strip()

    # 1. The answer
    console.print()
    console.print(Rule("[bold cyan]Answer[/]", style="cyan"))
    console.print()
    _print_markdown(answer_md or f"**Summary:** {report_dict.get('summary', 'No narrative answer generated.')}")
    console.print()

    # 2. Sources, after the answer, one block per citation
    refs = _parse_references(references_md)
    console.print(Rule(f"[bold green]Sources ({len(refs)})[/]" if refs else "[bold green]Sources[/]", style="green"))
    console.print()
    if refs:
        for ref in refs:
            console.print(f"[bold cyan][{ref['num']}][/] [bold]{escape(ref['title'])}[/]")
            console.print(f"    [link={ref['url']}]{escape(ref['url'])}[/link]", style="blue", soft_wrap=True)
            if ref["retrieved"]:
                console.print(f"    [dim]Retrieved {ref['retrieved']}[/]")
            if ref["quote"]:
                console.print(f"    [italic dim]“{escape(ref['quote'])}”[/]")
            console.print()
    elif references_md.strip():
        console.print(Markdown(references_md.strip()))
        console.print()
    else:
        console.print("[dim]No external citations.[/]\n")

    # 3. One-line run summary
    claims = report_dict.get("claims", [])
    grounded = sum(1 for c in claims if c.get("citations") or c.get("finding_ids"))
    parts = []
    if claims:
        color = "green" if grounded == len(claims) else "yellow"
        parts.append(f"[{color}]{grounded}/{len(claims)} claims cited[/]")
    if budget:
        tokens = budget.get("tokens_used", budget.get("tokens_in", 0) + budget.get("tokens_out", 0))
        parts.append(f"{budget.get('llm_calls', 0)} LLM calls")
        parts.append(f"{tokens:,} tokens")
        parts.append(f"{budget.get('searches', 0)} searches")
        if budget.get("elapsed_s"):
            parts.append(f"{budget['elapsed_s']:.0f}s")
    if run_id:
        parts.append(f"run {run_id}")
    if parts:
        console.print(Rule(style="dim"))
        console.print("[dim]" + " · ".join(parts) + "[/]")
    console.print()


def display_failure(data: dict) -> None:
    """Explains why a run did not produce a report, with a next step."""
    run_id = data.get("run_id", "")
    status = data.get("status", "failed")
    reason = data.get("termination_reason") or "unknown"
    color = "yellow" if status == "cancelled" else "red"
    lines = [f"[bold {color}]Run {status}[/] — reason: [bold]{reason}[/]"]
    if data.get("termination_detail"):
        lines.append(f"[dim]{escape(data['termination_detail'])}[/]")
    hint = _FAILURE_HINTS.get(reason)
    if hint and status != "cancelled":
        lines.append("")
        lines.append(hint.format(run_id=run_id))
    console.print(Panel("\n".join(lines), border_style=color, title=f"Run {run_id}"))


def _fetch_and_display_report(run_id: str, api_url: str) -> None:
    try:
        resp = httpx.get(f"{api_url}/research/{run_id}", timeout=10.0)
    except httpx.HTTPError as e:
        console.print(f"[dim]Could not fetch final report: {e}[/dim]")
        return
    if resp.status_code != 200:
        console.print(f"[bold red]Could not retrieve report:[/] HTTP {resp.status_code}")
        return
    data = resp.json()
    if data.get("report") and data["report"].get("markdown"):
        display_report_result(data["report"], budget=data.get("budget"), question=data.get("question"), run_id=run_id)
        if data.get("status") == "completed_partial":
            console.print(f"[yellow]Partial answer[/] ({data.get('termination_reason') or 'budget'}): see Coverage Gaps above.\n")
    elif data.get("status") in ("failed", "cancelled"):
        display_failure(data)
    else:
        console.print(f"[dim]No report yet for run '{run_id}'. Status: {data.get('status')}[/dim]")


def cmd_status(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    run_id = args.run_id

    resp = httpx.get(f"{api_url}/research/{run_id}", timeout=10.0)
    if resp.status_code == 404:
        console.print(f"[bold red]Error:[/] Run '{run_id}' not found.")
        sys.exit(1)

    data = resp.json()
    console.print(Panel(f"[bold]Run ID:[/] {data['run_id']}\n[bold]Status:[/] {data['status']}\n[bold]Question:[/] {data['question']}", title="Scout Run Status"))

    if data.get("report") and data["report"].get("markdown"):
        display_report_result(data["report"], budget=data.get("budget"), question=data.get("question"), run_id=run_id)
    else:
        if data.get("status") in ("failed", "cancelled"):
            display_failure(data)
        table = Table(title="Progress & Budget")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")
        table.add_row("Sub-questions Done", f"{data['progress']['subquestions_done']} / {data['progress']['subquestions_total']}")
        table.add_row("Tokens Used", f"{data['budget']['tokens_used']} / {data['budget']['tokens_max']}")
        table.add_row("LLM Calls", str(data['budget']['llm_calls']))
        table.add_row("Searches", str(data['budget']['searches']))
        table.add_row("Elapsed Time", f"{data['budget']['elapsed_s']} s")
        console.print(table)


def cmd_cancel(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    resp = httpx.post(f"{api_url}/research/{args.run_id}/cancel", timeout=10.0)
    console.print(f"Cancel Response ({resp.status_code}): {resp.json()}")


def cmd_resume(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    resp = httpx.post(f"{api_url}/research/{args.run_id}/resume", timeout=10.0)
    console.print(f"Resume Response ({resp.status_code}): {resp.json()}")


def cmd_trace(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    url = f"{api_url}/research/{args.run_id}/trace"
    if args.include_payloads:
        url += "?include=payloads"

    try:
        resp = httpx.get(url, timeout=10.0)
    except Exception as e:
        console.print(f"[bold red]Error connecting to Scout API:[/] {e}")
        sys.exit(1)

    if resp.status_code != 200:
        console.print(f"[bold red]Error ({resp.status_code}):[/] {resp.text}")
        sys.exit(1)

    data = resp.json()
    console.print(Panel(f"[bold]Run ID:[/] {data['run_id']}\n[bold]Total Steps:[/] {data['total_steps']}", title="Scout Execution Trace (PRD FR-5.5)"))

    table = Table(title="Step Log")
    table.add_column("Index", style="dim")
    table.add_column("Agent / Node", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Tool / Model", style="green")
    table.add_column("Status", style="bold")
    table.add_column("Latency (ms)", justify="right")
    table.add_column("Preview (Output)", style="white")

    for idx, step in enumerate(data.get("steps", []), start=1):
        status_style = "green" if step.get("status") == "ok" else "red"
        tool_or_model = step.get("tool_name") or step.get("model") or "-"
        preview = step.get("produced_output", {}).get("preview", "")[:60]
        table.add_row(
            str(idx),
            step.get("agent_node", "-"),
            step.get("step_kind", "-"),
            tool_or_model,
            f"[{status_style}]{step.get('status', 'ok')}[/]",
            f"{step.get('latency_ms', 0.0):.1f}",
            preview,
        )

    console.print(table)


def cmd_runs(args: argparse.Namespace) -> None:
    import redis
    from scout.store.redis_keys import key_runs_active, key_lease

    r = redis.Redis.from_url(args.redis_url)

    if args.action == "purge":
        active = r.smembers(key_runs_active())
        purged = 0
        for r_id_b in active:
            r_id = r_id_b.decode("utf-8")
            lease = r.get(key_lease(r_id))
            if not lease or args.force:
                r.srem(key_runs_active(), r_id)
                r.hset(f"run:{r_id}:meta", mapping={"status": "purged", "termination_reason": "manual_purge"})
                purged += 1
        console.print(f"[bold green]Purged {purged} stalled run(s) from active set.[/]")
        return

    # List runs
    table = Table(title="Scout Research Runs in Redis")
    table.add_column("Run ID", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Lease", style="green")
    table.add_column("Question", style="white")

    active_runs = {x.decode("utf-8") for x in r.smembers(key_runs_active())}
    all_meta_keys = r.keys("run:*:meta")
    seen_ids = set()

    for k in sorted(all_meta_keys):
        k_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
        parts = k_str.split(":")
        if len(parts) >= 3 and parts[0] == "run":
            run_id = parts[1]
            seen_ids.add(run_id)
            meta = {k.decode("utf-8"): v.decode("utf-8") for k, v in r.hgetall(k).items()}
            status = meta.get("status", "unknown")
            question = meta.get("question", "")[:60]
            has_lease = bool(r.get(key_lease(run_id)))

            status_style = "green" if status in ("completed", "completed_partial") else "yellow" if status == "running" else "red"
            lease_str = "[green]Active[/]" if has_lease else ("[red]Expired (Stalled)[/]" if run_id in active_runs else "[dim]None[/]")

            table.add_row(
                run_id,
                f"[{status_style}]{status}[/]",
                lease_str,
                question,
            )

    if not seen_ids:
        console.print("[dim]No runs found in Redis.[/]")
    else:
        console.print(table)


def cmd_worker(args: argparse.Namespace) -> None:
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    from scout.worker import ResearchWorker

    worker_id = f"worker_{os.getpid()}"
    console.print(f"[bold green]Starting Autonomous Research Worker:[/] {worker_id}")
    worker = ResearchWorker(redis_url=args.redis_url, worker_id=worker_id)
    stop_event = threading.Event()

    def signal_handler(sig, frame):
        console.print("\n[yellow]Shutdown signal received. Gracefully halting worker...[/]")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    worker.run_worker_daemon(stop_event=stop_event)


def _execute_and_display_query(question: str, api_url: str) -> None:
    question = question.strip()
    if not question:
        return

    console.print(Panel(f"[bold cyan]Researching Query:[/]\n{question}", title="Scout Research Run"))

    try:
        resp = httpx.post(
            f"{api_url}/research",
            json={"question": question},
            timeout=10.0,
        )
        if resp.status_code == 429:
            err = resp.json()
            console.print(
                f"[bold yellow]Admission Control Rejected (HTTP 429):[/] {err.get('code')}\n"
                f"Projected calls: {err.get('projected_calls')} > Remaining calls: {err.get('remaining_calls')}\n"
                f"Retry after: {err.get('retry_after_s')} seconds."
            )
            return
        elif resp.status_code != 202:
            console.print(f"[bold red]API Error ({resp.status_code}):[/] {resp.text}")
            return

        data = resp.json()
        run_id = data["run_id"]
        console.print(f"[bold green]Accepted Run ID:[/] {run_id} (status: [cyan]{data['status']}[/])")
        cmd_watch(argparse.Namespace(run_id=run_id, api_url=api_url))

    except (httpx.ConnectError, httpx.RequestError) as conn_err:
        console.print(f"[yellow]Notice: FastAPI server not reachable at {api_url} ({conn_err}).[/yellow]")
        console.print("[cyan]Falling back to direct local execution...[/cyan]")
        _execute_direct_local(question)


def _execute_direct_local(question: str, redis_url: str = "redis://127.0.0.1:6379/0") -> None:
    import redis
    from scout.worker import ResearchWorker
    from scout.store.ledgers import LedgerStore
    from scout.store.redis_keys import key_run_meta, key_run_report

    r = redis.Redis.from_url(redis_url)
    run_id = f"cli_{uuid.uuid4().hex[:10]}"
    r.hset(key_run_meta(run_id), mapping={"question": question, "status": "running"})

    console.print(f"[bold green]Direct Local Execution started (Run ID: {run_id})[/bold green]")
    worker = ResearchWorker(redis_url=redis_url, worker_id=f"cli_{os.getpid()}")
    try:
        final_state = worker.execute_run(run_id=run_id, initial_question=question)
        report_raw = r.get(key_run_report(run_id))
        report_dict = (
            json.loads(report_raw)
            if report_raw
            else (final_state.report.model_dump() if final_state.report else {})
        )
        # run:{id}:budget is a hash; LedgerStore parses it into ints
        budget_dict = LedgerStore(r).get_run_budget(run_id).model_dump()
        display_report_result(report_dict, budget=budget_dict, question=question, run_id=run_id)
    except Exception as e:
        console.print(f"[bold red]Direct execution error:[/] {e}")


def cmd_ask(args: argparse.Namespace) -> None:
    api_url = getattr(args, "api_url", DEFAULT_API_URL).rstrip("/")
    question = getattr(args, "question", None)

    if question:
        _execute_and_display_query(question, api_url=api_url)
        return

    # Interactive prompt loop
    console.print(
        Panel(
            "[bold cyan]🔭 Scout Interactive Research Assistant[/bold cyan]\n"
            "[white]Ask any research question. Scout will execute multi-agent planning, web search,\n"
            "evidence extraction, and synthesis to provide a real answer, followed by verified\n"
            "citations and validation integrity checks.[/white]\n\n"
            "[dim]Commands: Type your query and hit Enter. Type '[bold red]exit[/bold red]' or '[bold red]quit[/bold red]' to return to terminal.[/dim]",
            border_style="cyan",
            title="[bold]Scout CLI[/bold]",
            padding=(1, 2),
        )
    )

    while True:
        try:
            user_input = Prompt.ask("\n[bold cyan]Scout [Ask][/bold cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]Exiting Scout interactive session. Goodbye![/yellow]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q", ":q"):
            console.print("[dim]Exiting Scout interactive session. Goodbye![/dim]")
            break
        if user_input.lower() in ("help", "?"):
            console.print("[cyan]Tips:[/] Type any research question, e.g. 'Compare Redis and SQLite' or 'exit' to quit.")
            continue

        _execute_and_display_query(user_input, api_url=api_url)


def main() -> None:
    # If no arguments provided at all, enter interactive ask mode directly
    if len(sys.argv) == 1:
        cmd_ask(argparse.Namespace(question=None, api_url=DEFAULT_API_URL))
        return

    parser = argparse.ArgumentParser(prog="scout", description="Scout Multi-Agent Research Assistant CLI")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base URL of Scout FastAPI server")
    subparsers = parser.add_subparsers(dest="command", required=False)

    # ask (interactive or single query)
    p_ask = subparsers.add_parser("ask", help="Ask a research query or launch interactive shell")
    p_ask.add_argument("question", nargs="?", default=None, help="Question to research (omit to launch interactive shell)")
    p_ask.set_defaults(func=cmd_ask)

    # chat / interactive aliases
    p_chat = subparsers.add_parser("chat", help="Launch interactive research shell")
    p_chat.set_defaults(func=lambda args: cmd_ask(argparse.Namespace(question=None, api_url=args.api_url)))

    p_interactive = subparsers.add_parser("interactive", help="Launch interactive research shell")
    p_interactive.set_defaults(func=lambda args: cmd_ask(argparse.Namespace(question=None, api_url=args.api_url)))

    # run
    p_run = subparsers.add_parser("run", help="Submit a new research question")
    p_run.add_argument("question", help="Question to research")
    p_run.add_argument("--idempotency-key", default=None, help="Optional idempotency key")
    p_run.add_argument("--no-watch", action="store_true", help="Submit without streaming events")
    p_run.set_defaults(func=cmd_run)

    # watch
    p_watch = subparsers.add_parser("watch", help="Watch progress events from an active run")
    p_watch.add_argument("run_id", help="Research Run ID")
    p_watch.set_defaults(func=cmd_watch)

    # status
    p_status = subparsers.add_parser("status", help="Get run status and report")
    p_status.add_argument("run_id", help="Research Run ID")
    p_status.set_defaults(func=cmd_status)

    # trace
    p_trace = subparsers.add_parser("trace", help="Get ordered step trace for a run")
    p_trace.add_argument("run_id", help="Research Run ID")
    p_trace.add_argument("--include-payloads", action="store_true", help="Inline full payloads")
    p_trace.set_defaults(func=cmd_trace)

    # cancel
    p_cancel = subparsers.add_parser("cancel", help="Cancel an active research run")
    p_cancel.add_argument("run_id", help="Research Run ID")
    p_cancel.set_defaults(func=cmd_cancel)

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume a stalled research run")
    p_resume.add_argument("run_id", help="Research Run ID")
    p_resume.set_defaults(func=cmd_resume)

    # runs
    p_runs = subparsers.add_parser("runs", help="Inspect or purge research runs in Redis")
    p_runs.add_argument("action", choices=["list", "purge"], nargs="?", default="list", help="Action (list or purge)")
    p_runs.add_argument("--force", action="store_true", help="Force purge all active runs even if leased")
    p_runs.add_argument("--redis-url", default="redis://127.0.0.1:6379/0", help="Redis connection URL")
    p_runs.set_defaults(func=cmd_runs)

    # worker
    p_worker = subparsers.add_parser("worker", help="Run background worker daemon")
    p_worker.add_argument("--redis-url", default="redis://127.0.0.1:6379/0", help="Redis connection URL")
    p_worker.set_defaults(func=cmd_worker)

    args = parser.parse_args()
    if not hasattr(args, "func") or args.command is None:
        cmd_ask(argparse.Namespace(question=None, api_url=args.api_url))
        return

    args.func(args)


if __name__ == "__main__":
    main()
