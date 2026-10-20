"""Scout Command-Line Interface (PRD §8, §13).

Provides commands:
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
import signal
import sys
import threading
import time
from typing import Optional

import httpx
from rich.console import Console
from rich.panel import Panel
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


def cmd_watch(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    run_id = args.run_id
    last_event_id: Optional[str] = getattr(args, "last_event_id", None)
    max_reconnects = 10
    reconnect_delay = 2.0

    for attempt in range(max_reconnects + 1):
        events_url = f"{api_url}/research/{run_id}/events"
        params = {}
        headers = {}
        if last_event_id:
            params["last_event_id"] = last_event_id
            headers["Last-Event-ID"] = last_event_id

        if attempt == 0:
            console.print(f"[dim]Connecting to SSE stream: {events_url}[/]")
        else:
            console.print(f"[dim]Reconnecting to SSE stream (attempt {attempt}, last event ID: {last_event_id or 'none'})...[/]")

        try:
            with httpx.stream("GET", events_url, params=params, headers=headers, timeout=None) as response:
                if response.status_code == 404:
                    console.print(f"[bold red]Error:[/] Run '{run_id}' not found.")
                    return

                event_type = "progress"
                event_id = None
                data_buffer = []

                for line in response.iter_lines():
                    line = line.strip()
                    if not line:
                        if data_buffer:
                            raw_data = "\n".join(data_buffer)
                            data_buffer = []
                            _handle_event(event_type, event_id, raw_data)
                            if event_type in ("run_completed", "run_completed_partial", "run_failed", "run_cancelled"):
                                return
                        event_type = "progress"
                        event_id = None
                        continue

                    if line.startswith("id:"):
                        event_id = line[3:].strip()
                        last_event_id = event_id
                    elif line.startswith("event:"):
                        event_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_buffer.append(line[5:].strip())

            # Normal stream termination
            return

        except KeyboardInterrupt:
            console.print(f"\n[yellow]Interrupted by user.[/]")
            return
        except (httpx.RequestError, httpx.HTTPError) as e:
            # Check if run is still in progress
            try:
                st = httpx.get(f"{api_url}/research/{run_id}", timeout=5.0).json()
                if st.get("status") in ("completed", "completed_partial", "failed", "cancelled"):
                    console.print(f"[bold green]Run reached terminal state:[/] {st.get('status')}")
                    return
            except Exception:
                pass

            if attempt < max_reconnects:
                time.sleep(reconnect_delay)
                continue
            else:
                console.print(
                    f"\n[dim]Disconnected from stream ({e}). Reconnect with: scout watch {run_id} "
                    f"(last event ID: {last_event_id or 'none'})[/]"
                )
                return


def _handle_event(event_type: str, event_id: Optional[str], raw_data: str) -> None:
    try:
        payload = json.loads(raw_data)
    except Exception:
        payload = {"raw": raw_data}

    ts = time.strftime("%H:%M:%S")
    if event_type == "run_started":
        console.print(f"[{ts}] [bold green]🚀 Run Started[/] question: {payload.get('question')}")
    elif event_type == "plan_created":
        subqs = payload.get("plan", {}).get("sub_questions", [])
        console.print(f"[{ts}] [bold blue]📋 Plan Generated[/] ({len(subqs)} sub-questions)")
        for sq in subqs:
            console.print(f"     • [cyan]{sq.get('id')}[/]: {sq.get('question')}")
    elif event_type == "subquestion_started":
        console.print(f"[{ts}] [yellow]🔍 Investigating[/] sub-question: {payload.get('subquestion_id')}")
    elif event_type == "search_completed":
        console.print(f"[{ts}] [dim]🌐 Search completed[/] for: {payload.get('query')}")
    elif event_type == "page_fetched":
        console.print(f"[{ts}] [dim]📄 Page fetched[/]: {payload.get('url')} (status: {payload.get('status')})")
    elif event_type == "findings_extracted":
        count = payload.get("count", 0)
        console.print(f"[{ts}] [bold magenta]💡 Verified Findings[/]: extracted {count} findings")
    elif event_type == "review_decision":
        decision = payload.get("decision")
        color = "green" if decision == "accept" else "yellow" if decision == "rework" else "red"
        console.print(f"[{ts}] [bold]🧐 Review Decision:[/] [{color}]{decision}[/] for {payload.get('subquestion_id')}")
    elif event_type == "budget_warning":
        console.print(f"[{ts}] [bold orange3]⚠️ Budget Warning:[/] {payload.get('message')}")
    elif event_type == "writing_started":
        console.print(f"[{ts}] [bold cyan]✍️ Writer Synthesizing Report[/]")
    elif event_type == "validation_result":
        valid = payload.get("valid")
        console.print(f"[{ts}] [bold]🔍 Citation Validation:[/] valid={valid}")
    elif event_type in ("run_completed", "run_completed_partial"):
        console.print(f"\n[{ts}] [bold green]✅ Research Run Finished ({event_type})![/]")
    elif event_type in ("run_failed", "run_cancelled"):
        console.print(f"\n[{ts}] [bold red]🛑 Research Run Ended ({event_type}):[/] {payload.get('reason')}")
    elif event_type == "heartbeat":
        pass
    else:
        console.print(f"[{ts}] [dim]{event_type}[/]: {payload}")


def cmd_status(args: argparse.Namespace) -> None:
    api_url = args.api_url.rstrip("/")
    run_id = args.run_id

    resp = httpx.get(f"{api_url}/research/{run_id}", timeout=10.0)
    if resp.status_code == 404:
        console.print(f"[bold red]Error:[/] Run '{run_id}' not found.")
        sys.exit(1)

    data = resp.json()
    console.print(Panel(f"[bold]Run ID:[/] {data['run_id']}\n[bold]Status:[/] {data['status']}\n[bold]Question:[/] {data['question']}", title="Scout Run Status"))

    table = Table(title="Progress & Budget")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Sub-questions Done", f"{data['progress']['subquestions_done']} / {data['progress']['subquestions_total']}")
    table.add_row("Tokens Used", f"{data['budget']['tokens_used']} / {data['budget']['tokens_max']}")
    table.add_row("LLM Calls", str(data['budget']['llm_calls']))
    table.add_row("Searches", str(data['budget']['searches']))
    table.add_row("Elapsed Time", f"{data['budget']['elapsed_s']} s")
    console.print(table)

    if data.get("report") and data["report"].get("markdown"):
        console.print("\n[bold green]Report Markdown Preview:[/]")
        console.print(Panel(data["report"]["markdown"][:1000] + ("..." if len(data["report"]["markdown"]) > 1000 else "")))


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="scout", description="Scout Multi-Agent Research Assistant CLI")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="Base URL of Scout FastAPI server")
    subparsers = parser.add_subparsers(dest="command", required=True)

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
    args.func(args)


if __name__ == "__main__":
    main()
