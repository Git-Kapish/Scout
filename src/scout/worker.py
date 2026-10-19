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

from scout.effects.fault_injection import maybe_crash
from scout.graph.builder import create_research_graph
from scout.graph.state import ResearchState
from scout.store.export import RunExporter
from scout.store.ledgers import LedgerStore
from scout.store.lua_scripts import ScriptRegistry
from scout.store.redis_keys import (
    key_lease,
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

logger = logging.getLogger(__name__)


class StaleWorkerError(Exception):
    """Raised when a worker's fencing generation has been superseded by another worker."""
    pass


class LeaseAcquisitionError(Exception):
    """Raised when a worker fails to acquire an active lease."""
    pass



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
            with RedisSaver.from_conn_string(self.redis_url) as checkpointer:
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
                else:
                    logger.info("Starting FRESH run '%s'", run_id)
                    config = {"configurable": {"thread_id": run_id}}
                    input_state = ResearchState(
                        run_id=run_id,
                        question=initial_question or "",
                        options=options or {},
                        cancellation_requested=bool(options and options.get("cancellation_requested")),
                    )
                    # Persist initial metadata
                    self.registry.fenced_hset(
                        gen_k,
                        meta_k,
                        generation,
                        {
                            "run_id": run_id,
                            "status": "running",
                            "question": initial_question or "",
                            "created_at_ts": str(time.time()),
                        },
                    )

                # Execute graph node-by-node via stream
                last_state: Optional[ResearchState] = None
                for update in app.stream(input_state, config=config, stream_mode="updates"):
                    # update is a dict: {node_name: {changed_state_keys...}}
                    node_name = next(iter(update.keys())) if update else "unknown"
                    current_seq += 1

                    # Get latest saved checkpoint from state snapshot (synchronous & reliable)
                    st_snapshot = app.get_state({"configurable": {"thread_id": run_id}})
                    latest_cid = (
                        st_snapshot.config.get("configurable", {}).get("checkpoint_id")
                        if st_snapshot and st_snapshot.config
                        else None
                    )
                    if not latest_cid:
                        tup = checkpointer.get_tuple({"configurable": {"thread_id": run_id}})
                        latest_cid = tup.checkpoint["id"] if tup and tup.checkpoint else None

                    if not latest_cid or latest_cid == "unknown_cid":
                        logger.debug("Skipping commit pointer advancement for uncheckpointed node: %s", node_name)
                        continue

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

                    # Fenced sync of application read-model
                    node_output = update[node_name]
                    self._sync_read_model(run_id, generation, node_name, node_output)

                # Final state retrieval (query latest without pinning checkpoint_id)
                final_config = {"configurable": {"thread_id": run_id}}
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

        finally:
            self.stop_heartbeat(run_id)

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
            {"event": "node_completed", "node": node_name, "timestamp": str(time.time())},
        )

        # 5. Emit trace entry to stream
        trace_k = key_run_trace(run_id)
        self.registry.fenced_xadd(
            gen_k,
            trace_k,
            generation,
            {"step": node_name, "status": "ok", "timestamp": str(time.time())},
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
                    self.execute_run(run_id, force_lease=True)
                    resumed.append(run_id)
                except Exception as e:
                    logger.error("Failed to resume stalled run '%s': %s", run_id, e)
        return resumed
