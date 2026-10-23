"""Research Worker Daemon with Lease Heartbeat and Fenced Resume (PRD FR-4.1, FR-4.4).

Manages execution of research runs under atomic Redis fencing:
1. Acquires lease and obtains monotonically increasing fencing generation.
2. Runs periodic heartbeat loop to renew lease.
3. Checkpoints state using LangGraph RedisSaver (thread_id = run_id).
4. Advances atomic commit pointer (`run:{id}:committed`) via Lua script.
5. Resumes strictly from the checkpoint recorded in `run:{id}:committed`.
6. Stale workers whose generation was superseded are rejected and halted immediately.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

import redis
from langgraph.checkpoint.redis import RedisSaver

from scout.config import get_settings
from scout.effects.fault_injection import maybe_crash
from scout.effects.journal import EffectJournal
from scout.effects.recorder import ReplayMissError
from scout.effects.runtime import RunHooks, run_hooks
from scout.graph.builder import create_research_graph
from scout.graph.state import ResearchState
from scout.llm.client import classify_provider_failure
from scout.store.export import RunExporter
from scout.store.ledgers import LedgerStore
from scout.store.lua_scripts import ScriptRegistry
from scout.store.redis_keys import (
    key_lease,
    key_queue_runs,
    key_run_budget,
    key_run_committed,
    key_run_events,
    key_run_findings,
    key_run_generation,
    key_run_meta,
    key_run_report,
    key_run_trace,
    key_runs_active,
)
from scout.trace.store import TraceStore

logger = logging.getLogger(__name__)


class StaleWorkerError(Exception):
    """Raised when a worker's fencing generation has been superseded by another worker."""
    pass


class LeaseAcquisitionError(Exception):
    """Raised when a worker fails to acquire an active lease."""
    pass



def _progress_fields(node_output: Dict[str, Any]) -> Dict[str, str]:
    """Small human-readable details for node_completed events (CLI progress display)."""
    fields: Dict[str, str] = {}
    plan = node_output.get("plan")
    if plan is not None and getattr(plan, "sub_questions", None):
        fields["subquestions"] = json.dumps({sq.id: sq.question for sq in plan.sub_questions})
    if node_output.get("active_subquestion_id"):
        fields["subquestion_id"] = node_output["active_subquestion_id"]
    if "findings" in node_output:
        fields["findings"] = str(len(node_output["findings"] or []))
    if "completed_subquestion_ids" in node_output:
        fields["accepted"] = str(len(node_output["completed_subquestion_ids"]))
        fields["abandoned"] = str(len(node_output.get("abandoned_subquestion_ids") or []))
    return fields


class ResearchWorker:
    """Executes research runs with durable state, atomic fencing, and lease renewal."""

    def __init__(
        self,
        redis_url: str = "redis://127.0.0.1:6379/0",
        worker_id: Optional[str] = None,
        lease_ttl_s: int = 15,
        heartbeat_interval_s: float = 2.0,
    ):
        self.redis_url = redis_url
        self.worker_id = worker_id or f"worker_{uuid.uuid4().hex[:8]}"
        self.lease_ttl_s = lease_ttl_s
        self.heartbeat_interval_s = heartbeat_interval_s

        self.r = redis.Redis.from_url(redis_url)
        self.registry = ScriptRegistry(self.r)
        self.ledgers = LedgerStore(self.r)
        self.exporter = RunExporter(self.r)
        self.trace_store = TraceStore(self.r)

        self._heartbeat_stop_events: Dict[str, threading.Event] = {}
        self._heartbeat_threads: Dict[str, threading.Thread] = {}

    def start_heartbeat(self, run_id: str, generation: int) -> None:
        """Starts a background thread to periodically renew the worker lease."""
        stop_event = threading.Event()
        self._heartbeat_stop_events[run_id] = stop_event

        def _heartbeat_loop():
            lease_k = key_lease(run_id)
            gen_k = key_run_generation(run_id)
            while not stop_event.is_set():
                if stop_event.wait(timeout=self.heartbeat_interval_s):
                    break
                ok, reason = self.registry.renew_lease(
                    lease_key=lease_k,
                    gen_key=gen_k,
                    worker_id=self.worker_id,
                    generation=generation,
                    ttl_seconds=self.lease_ttl_s,
                )
                if not ok:
                    logger.warning(
                        "Worker %s lost lease for run %s: reason=%s",
                        self.worker_id,
                        run_id,
                        reason,
                    )
                    break

        t = threading.Thread(target=_heartbeat_loop, daemon=True, name=f"hb_{run_id}")
        self._heartbeat_threads[run_id] = t
        t.start()

    def _run_hooks(self, run_id: str) -> RunHooks:
        """Wires agent calls to this run's cancellation flag, provider ledger and effect journal."""
        meta_k = key_run_meta(run_id)
        journal = EffectJournal(self.r)
        policy = get_settings().effects.read_only_ambiguity_policy
        return RunHooks(
            is_cancelled=lambda: self.r.hget(meta_k, "cancellation_requested") == b"1",
            record_provider_usage=lambda provider, requests, tokens: self.ledgers.record_provider_usage(
                provider, requests=requests, tokens=tokens
            ),
            run_effect=lambda step, effect_type, inputs, dispatch, ser, deser: journal.execute_durable(
                run_id, step, effect_type, inputs, dispatch, ser, deser, ambiguity_policy=policy
            )[0],
        )

    def stop_heartbeat(self, run_id: str) -> None:
        """Stops the background heartbeat loop."""
        stop_event = self._heartbeat_stop_events.pop(run_id, None)
        if stop_event:
            stop_event.set()
        t = self._heartbeat_threads.pop(run_id, None)
        if t and t.is_alive():
            t.join(timeout=2.0)

    def execute_run(
        self,
        run_id: str,
        initial_question: Optional[str] = None,
        options: Optional[Dict[str, Any]] = None,
        force_lease: bool = False,
        planner_agent=None,
        researcher_agent=None,
        writer_agent=None,
        supervisor_reviewer=None,
    ) -> ResearchState:
        """Executes or resumes a research run with atomic fencing and checkpoint commits."""
        lease_k = key_lease(run_id)
        gen_k = key_run_generation(run_id)
        active_k = key_runs_active()
        commit_k = key_run_committed(run_id)
        meta_k = key_run_meta(run_id)

        # 1. Acquire Lease & Monotonic Generation Bump
        ok, reason, generation = self.registry.acquire_lease(
            lease_key=lease_k,
            gen_key=gen_k,
            active_runs_key=active_k,
            worker_id=self.worker_id,
            ttl_seconds=self.lease_ttl_s,
            run_id=run_id,
            force=force_lease,
        )
        if not ok:
            raise LeaseAcquisitionError(f"Could not acquire lease for run '{run_id}': {reason}")

        logger.info(
            "Worker '%s' acquired lease for run '%s' at generation %d",
            self.worker_id,
            run_id,
            generation,
        )

        self.start_heartbeat(run_id, generation)

        try:
            with RedisSaver.from_conn_string(self.redis_url) as checkpointer, run_hooks(self._run_hooks(run_id)):
                checkpointer.setup()
                app = create_research_graph(
                    planner_agent=planner_agent,
                    researcher_agent=researcher_agent,
                    writer_agent=writer_agent,
                    supervisor_reviewer=supervisor_reviewer,
                    checkpointer=checkpointer,
                )

                # Check if this is a resume
                committed_raw = self.r.get(commit_k)
                committed_data = json.loads(committed_raw) if committed_raw else None

                current_seq = 0
                if committed_data and "checkpoint_id" in committed_data:
                    target_cid = committed_data["checkpoint_id"]
                    current_seq = committed_data.get("seq", 0)
                    logger.info(
                        "RESUMING run '%s' from committed checkpoint '%s' (seq %d)",
                        run_id,
                        target_cid,
                        current_seq,
                    )
                    config = {
                        "configurable": {
                            "thread_id": run_id,
                            "checkpoint_id": target_cid,
                        }
                    }
                    input_state = None
                    self.registry.fenced_hset(gen_k, meta_k, generation, {"status": "running", "termination_reason": ""})
                else:
                    logger.info("Starting FRESH run '%s'", run_id)
                    config = {"configurable": {"thread_id": run_id}}
                    # Preserve existing question from metadata if available
                    if not initial_question:
                        stored_q = self.r.hget(meta_k, "question")
                        if stored_q:
                            initial_question = stored_q.decode("utf-8") if isinstance(stored_q, bytes) else str(stored_q)

                    input_state = ResearchState(
                        run_id=run_id,
                        question=initial_question or "",
                        options=options or {},
                        cancellation_requested=bool(options and options.get("cancellation_requested")),
                    )
                    # Persist initial metadata
                    meta_updates = {
                        "run_id": run_id,
                        "status": "running",
                    }
                    if initial_question:
                        meta_updates["question"] = initial_question
                    self.registry.fenced_hset(
                        gen_k,
                        meta_k,
                        generation,
                        meta_updates,
                    )

                # Execute graph node-by-node via stream
                last_state: Optional[ResearchState] = None
                last_cid = committed_data.get("checkpoint_id") if committed_data else None
                pending_update: Optional[Dict[str, Any]] = None
                try:
                    # durability="sync": each checkpoint is persisted before its event reaches us.
                    for mode, chunk in app.stream(
                        input_state, config=config, stream_mode=["updates", "checkpoints"], durability="sync"
                    ):
                        if mode == "updates":
                            pending_update = chunk  # {node_name: {changed_state_keys...}}
                            continue
                        if pending_update is None:
                            continue  # input checkpoint: no node output to commit
                        update, pending_update = pending_update, None
                        node_name = next(iter(update.keys())) if update else "unknown"
                        current_seq += 1

                        # Commit this step's own checkpoint, never the thread's latest: a stale worker
                        # may have written newer library checkpoints for the same thread (PRD FR-4.1).
                        latest_cid = chunk["config"]["configurable"]["checkpoint_id"]

                        # Crash hook 5: after_checkpoint_before_commit
                        maybe_crash("after_checkpoint_before_commit")

                        # Advance Atomic Commit Pointer (PRD FR-4.1)
                        committed_payload = {
                            "checkpoint_id": latest_cid,
                            "generation": generation,
                            "seq": current_seq,
                            "node": node_name,
                            "timestamp": time.time(),
                        }
                        commit_ok, commit_reason = self.registry.advance_commit(
                            gen_key=gen_k,
                            commit_key=commit_k,
                            generation=generation,
                            seq=current_seq,
                            committed_payload=committed_payload,
                        )

                        if not commit_ok:
                            if commit_reason == "stale_generation":
                                logger.error(
                                    "Worker %s generation %d is STALE for run %s. Halting.",
                                    self.worker_id,
                                    generation,
                                    run_id,
                                )
                                raise StaleWorkerError(
                                    f"Worker generation {generation} superseded for run '{run_id}'"
                                )
                            else:
                                logger.warning(
                                    "Commit pointer advancement rejected: %s", commit_reason
                                )
                        else:
                            last_cid = latest_cid

                        # Fenced sync of application read-model
                        node_output = update[node_name]
                        self._sync_read_model(run_id, generation, node_name, node_output)

                    # Final state comes from the last committed checkpoint, not the thread's latest
                    final_config = {"configurable": {"thread_id": run_id}}
                    if last_cid:
                        final_config["configurable"]["checkpoint_id"] = last_cid
                    final_snapshot = app.get_state(final_config)
                    if final_snapshot and final_snapshot.values:
                        last_state = ResearchState.model_validate(final_snapshot.values)
                    else:
                        final_tuples = list(checkpointer.list({"configurable": {"thread_id": run_id}}))
                        if final_tuples:
                            channel_values = final_tuples[0].checkpoint.get("channel_values", {})
                            if channel_values:
                                last_state = ResearchState.model_validate(channel_values)

                    # Mark complete & Export
                    if last_state:
                        self.registry.fenced_hset(
                            gen_k,
                            meta_k,
                            generation,
                            {
                                "status": last_state.status,
                                "completed_at_ts": str(time.time()),
                                "termination_reason": last_state.termination_reason or "",
                            },
                        )

                    self._emit_terminal_event(
                        run_id,
                        generation,
                        last_state.status if last_state else "failed",
                        (last_state.termination_reason if last_state else None) or "",
                    )
                    self.exporter.export_run(run_id)

                    # Release lease as terminal
                    self.registry.release_lease(
                        lease_key=lease_k,
                        gen_key=gen_k,
                        active_runs_key=active_k,
                        worker_id=self.worker_id,
                        generation=generation,
                        run_id=run_id,
                        is_terminal=True,
                    )

                    return last_state or ResearchState(run_id=run_id, question="")

                except Exception as exc:
                    logger.error("Run %s encountered unhandled error during execution: %s", run_id, exc)
                    # Provider and replay failures are structured reasons; quota_limited and
                    # provider_unavailable stay resumable from the last committed checkpoint (FR-4.4).
                    if isinstance(exc, ReplayMissError):
                        reason = "replay_miss"
                    else:
                        reason = classify_provider_failure(exc) or "unhandled_error"
                    self.registry.fenced_hset(
                        gen_k,
                        meta_k,
                        generation,
                        {
                            "status": "failed",
                            "completed_at_ts": str(time.time()),
                            "termination_reason": reason,
                            "termination_detail": str(exc)[:200],
                        },
                    )
                    self._emit_terminal_event(run_id, generation, "failed", reason, str(exc)[:200])
                    self.exporter.export_run(run_id)
                    self.registry.release_lease(
                        lease_key=lease_k,
                        gen_key=gen_k,
                        active_runs_key=active_k,
                        worker_id=self.worker_id,
                        generation=generation,
                        run_id=run_id,
                        is_terminal=True,
                    )
                    raise

        finally:
            self.stop_heartbeat(run_id)

    def _emit_terminal_event(
        self, run_id: str, generation: int, status: str, reason: str, detail: str = ""
    ) -> None:
        """Tells SSE clients the run ended so they can show the report or the failure reason."""
        event = "run_failed" if status == "running" else f"run_{status}"
        self.registry.fenced_xadd(
            key_run_generation(run_id),
            key_run_events(run_id),
            generation,
            {"event": event, "reason": reason, "detail": detail, "timestamp": str(time.time())},
        )

    def _sync_read_model(
        self,
        run_id: str,
        generation: int,
        node_name: str,
        node_output: Optional[Dict[str, Any]],
    ) -> None:
        """Syncs node output to Redis read-model keys guarded by generation fencing."""
        if not node_output or not isinstance(node_output, dict):
            return

        gen_k = key_run_generation(run_id)

        # 1. Update findings
        if "findings" in node_output:
            new_findings = node_output["findings"]
            findings_k = key_run_findings(run_id)
            if new_findings:
                mapping = {f.finding_id: f.model_dump_json() for f in new_findings}
                self.registry.fenced_hset(gen_k, findings_k, generation, mapping)

        # 2. Update report
        if "report" in node_output and node_output["report"] is not None:
            report_k = key_run_report(run_id)
            report_obj = node_output["report"]
            report_str = report_obj.model_dump_json() if hasattr(report_obj, "model_dump_json") else json.dumps(report_obj)
            self.registry.fenced_set(gen_k, report_k, generation, report_str)

        # 3. Update budget ledger
        if "budget_ledger" in node_output and node_output["budget_ledger"] is not None:
            ledger = node_output["budget_ledger"]
            b_key = key_run_budget(run_id)
            mapping = {
                "tokens_in": str(getattr(ledger, "tokens_in", 0)),
                "tokens_out": str(getattr(ledger, "tokens_out", 0)),
                "llm_calls": str(getattr(ledger, "llm_calls", 0)),
                "searches": str(getattr(ledger, "searches", 0)),
                "fetches": str(getattr(ledger, "fetches", 0)),
                "search_credits": str(getattr(ledger, "search_credits", 0.0)),
            }
            self.registry.fenced_hset(gen_k, b_key, generation, mapping)

        # 4. Emit progress event to stream
        events_k = key_run_events(run_id)
        self.registry.fenced_xadd(
            gen_k,
            events_k,
            generation,
            {"event": "node_completed", "node": node_name, **_progress_fields(node_output), "timestamp": str(time.time())},
        )
        # Budget events (PRD FR-2.2): pick_next flags the 80% warning and reports guard stops.
        if node_output.get("budget_warning_emitted"):
            self.registry.fenced_xadd(
                gen_k, events_k, generation, {"event": "budget_warning", "timestamp": str(time.time())}
            )
        if node_output.get("termination_reason") in ("wall_clock_timeout", "llm_calls_exhausted", "token_budget_exhausted"):
            self.registry.fenced_xadd(
                gen_k,
                events_k,
                generation,
                {"event": "budget_exhausted", "reason": node_output["termination_reason"], "timestamp": str(time.time())},
            )

        # 5. Emit structured trace entry (PRD FR-5.5)
        step_kind = "decision"
        if node_name in ("plan", "write"):
            step_kind = "llm"
        elif node_name == "research":
            step_kind = "tool"
        elif node_name == "validate_citations":
            step_kind = "validation"

        self.trace_store.record_node_step(
            run_id=run_id,
            agent_node=node_name,
            step_kind=step_kind,
            input_data={"node": node_name, "generation": generation},
            output_data=node_output,
            generation=generation,
            status="ok",
        )

    def scan_and_resume_stalled_runs(self) -> List[str]:
        """Scans runs:active for expired leases and triggers recovery."""
        active_runs = self.r.smembers(key_runs_active())
        resumed = []
        for r_id_bytes in active_runs:
            run_id = r_id_bytes.decode("utf-8")
            lease_val = self.r.get(key_lease(run_id))
            if not lease_val:
                logger.info("Found stalled run '%s' with expired lease. Resuming.", run_id)
                try:
                    meta_raw = self.r.hgetall(key_run_meta(run_id))
                    meta = {
                        (k.decode("utf-8") if isinstance(k, bytes) else str(k)): (
                            v.decode("utf-8") if isinstance(v, bytes) else str(v)
                        )
                        for k, v in meta_raw.items()
                    }
                    question = meta.get("question", "")
                    options_str = meta.get("options", "{}")
                    try:
                        options = json.loads(options_str) if options_str else {}
                    except Exception:
                        options = {}
                    if meta.get("cancellation_requested") == "1":
                        options["cancellation_requested"] = True

                    attempts_str = meta.get("resume_attempts", "0")
                    try:
                        resume_attempts = int(attempts_str)
                    except ValueError:
                        resume_attempts = 0

                    if resume_attempts >= 3:
                        logger.warning(
                            "Stalled run '%s' exceeded max resume attempts (%d). Marking as failed.",
                            run_id,
                            resume_attempts,
                        )
                        self.r.hset(
                            key_run_meta(run_id),
                            mapping={
                                "status": "failed",
                                "termination_reason": "max_resume_attempts_exceeded",
                                "completed_at_ts": str(time.time()),
                            },
                        )
                        self.r.srem(key_runs_active(), run_id)
                        self.exporter.export_run(run_id)
                        continue

                    self.r.hincrby(key_run_meta(run_id), "resume_attempts", 1)

                    self.execute_run(
                        run_id=run_id,
                        initial_question=question,
                        options=options,
                        force_lease=True,
                    )
                    resumed.append(run_id)
                except Exception as e:
                    logger.error("Failed to resume stalled run '%s': %s", run_id, e)
        return resumed

    def process_next_run(self, **agent_kwargs) -> Optional[ResearchState]:
        """Pops and executes the next pending run from queue:runs."""
        raw_run_id = self.r.lpop(key_queue_runs())
        if not raw_run_id:
            return None

        run_id = raw_run_id.decode("utf-8") if isinstance(raw_run_id, bytes) else str(raw_run_id)
        logger.info("Worker '%s' picked up queued run '%s'", self.worker_id, run_id)

        meta_raw = self.r.hgetall(key_run_meta(run_id))
        meta = {
            (k.decode("utf-8") if isinstance(k, bytes) else str(k)): (
                v.decode("utf-8") if isinstance(v, bytes) else str(v)
            )
            for k, v in meta_raw.items()
        }

        question = meta.get("question", "")
        options_str = meta.get("options", "{}")
        try:
            options = json.loads(options_str) if options_str else {}
        except Exception:
            options = {}

        if meta.get("cancellation_requested") == "1":
            options["cancellation_requested"] = True

        return self.execute_run(
            run_id=run_id,
            initial_question=question,
            options=options,
            **agent_kwargs,
        )

    def run_worker_daemon(
        self,
        stop_event: Optional[threading.Event] = None,
        poll_interval_s: float = 0.5,
        scan_interval_s: float = 5.0,
        **agent_kwargs,
    ) -> None:
        """Runs the continuous worker daemon processing queue and scanning stalled runs."""
        logger.info("Worker '%s' daemon starting", self.worker_id)
        stop = stop_event or threading.Event()
        last_scan_ts = 0.0

        while not stop.is_set():
            # 1. Process queued runs
            try:
                state = self.process_next_run(**agent_kwargs)
                if state:
                    continue  # immediately check for more work
            except Exception as e:
                logger.error("Error processing run in worker daemon: %s", e)

            # 2. Periodically scan and recover stalled runs
            now = time.time()
            if now - last_scan_ts >= scan_interval_s:
                last_scan_ts = now
                try:
                    self.scan_and_resume_stalled_runs()
                except Exception as e:
                    logger.error("Error scanning stalled runs: %s", e)

            # 3. Idle wait
            stop.wait(poll_interval_s)

        logger.info("Worker '%s' daemon stopped", self.worker_id)

