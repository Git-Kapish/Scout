"""Scout Research API Endpoints (PRD §8, FR-5.1 - FR-5.3).

Provides:
- POST /research: Accepts research request with optional Idempotency-Key and admission control (HTTP 202 or 429).
- GET /research/{run_id}: Canonical status, progress, budget, and report payload (HTTP 200).
- GET /research/{run_id}/events: Server-Sent Events (SSE) stream supporting Last-Event-ID reconnection replay.
- POST /research/{run_id}/cancel: Cooperative cancellation request.
- POST /research/{run_id}/resume: Recovers recoverable runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import redis
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from scout.api.admission import AdmissionController
from scout.api.schemas import (
    ActionResponse,
    AdmissionErrorResponse,
    BudgetInfo,
    ClaimData,
    ProgressInfo,
    ReportData,
    ResearchAcceptedLinks,
    ResearchAcceptedResponse,
    ResearchRequest,
    ResearchStatusResponse,
    SourceData,
)
from scout.config import get_settings
from scout.store.redis_keys import (
    key_idempotency,
    key_queue_runs,
    key_run_budget,
    key_run_events,
    key_run_findings,
    key_run_meta,
    key_run_report,
)
from scout.trace import ResearchTraceResponse, TraceStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/research", tags=["Research"])

RECOVERABLE_REASONS = {"worker_lost", "provider_unavailable", "quota_limited"}


def get_redis_client() -> redis.Redis:
    settings = get_settings()
    return redis.Redis.from_url(settings.redis.url)


def _decode(val: Any) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8")
    return str(val) if val is not None else ""


@router.post(
    "",
    status_code=202,
    response_model=ResearchAcceptedResponse,
    responses={
        202: {"model": ResearchAcceptedResponse, "description": "Run accepted and queued"},
        429: {"model": AdmissionErrorResponse, "description": "Daily allowance exceeded"},
    },
)
def create_research_run(
    request_data: ResearchRequest,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
) -> Any:
    """Submits a research question for investigation with admission control and idempotency."""
    r = get_redis_client()
    controller = AdmissionController(r)

    # 1. Idempotency Check (PRD §8, §9.2)
    if idempotency_key:
        idem_k = key_idempotency(idempotency_key)
        existing_run_id = r.get(idem_k)
        if existing_run_id:
            run_id = _decode(existing_run_id)
            logger.info("Idempotent request match: key '%s' -> run '%s'", idempotency_key, run_id)
            return ResearchAcceptedResponse(
                run_id=run_id,
                status="queued",
                links=ResearchAcceptedLinks(
                    status=f"/research/{run_id}",
                    events=f"/research/{run_id}/events",
                    trace=f"/research/{run_id}/trace",
                ),
            )

    # 2. Admission Control (PRD §5.2, §8)
    admitted, admission_err = controller.check_admission(request_data.options)
    if not admitted and admission_err is not None:
        return JSONResponse(
            status_code=429,
            content=admission_err.model_dump(),
            headers={"Retry-After": str(admission_err.retry_after_s)},
        )

    # 3. Create New Run
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    now_ts = time.time()

    if idempotency_key:
        # Cache Idempotency Key with 24h TTL
        r.set(key_idempotency(idempotency_key), run_id, ex=86400)

    # Persist initial metadata
    options_json = request_data.options.model_dump_json() if request_data.options else "{}"
    meta_payload = {
        "run_id": run_id,
        "status": "queued",
        "question": request_data.question,
        "created_at_ts": str(now_ts),
        "options": options_json,
    }
    r.hset(key_run_meta(run_id), mapping=meta_payload)

    # Enqueue in queue:runs
    r.rpush(key_queue_runs(), run_id)

    # Emit run_started event to stream
    r.xadd(
        key_run_events(run_id),
        {
            "event": "run_started",
            "run_id": run_id,
            "question": request_data.question,
            "timestamp": str(now_ts),
        },
    )

    logger.info("Enqueued research run '%s' for question: '%s'", run_id, request_data.question)
    return ResearchAcceptedResponse(
        run_id=run_id,
        status="queued",
        links=ResearchAcceptedLinks(
            status=f"/research/{run_id}",
            events=f"/research/{run_id}/events",
            trace=f"/research/{run_id}/trace",
        ),
    )


@router.get("/{run_id}", response_model=ResearchStatusResponse)
def get_research_status(run_id: str) -> ResearchStatusResponse:
    """Returns canonical status, progress, budget, and report payload (PRD §8)."""
    r = get_redis_client()
    meta_raw = r.hgetall(key_run_meta(run_id))

    if not meta_raw:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Research run '{run_id}' not found"},
        )

    meta = {_decode(k): _decode(v) for k, v in meta_raw.items()}
    status = meta.get("status", "queued")
    question = meta.get("question", "")
    termination_reason = meta.get("termination_reason")

    # Progress
    total_sq = int(meta.get("subquestions_total", 0))
    done_sq = int(meta.get("subquestions_done", 0))

    # Budget
    budget_raw = r.hgetall(key_run_budget(run_id))
    budget_dict = {_decode(k): _decode(v) for k, v in budget_raw.items()}

    tokens_in = int(budget_dict.get("tokens_in", 0))
    tokens_out = int(budget_dict.get("tokens_out", 0))
    llm_calls = int(budget_dict.get("llm_calls", 0))
    searches = int(budget_dict.get("searches", 0))

    created_at = float(meta.get("created_at_ts", time.time()))
    completed_at = float(meta.get("completed_at_ts", time.time()))
    elapsed_s = round(
        (completed_at if status in ("completed", "completed_partial", "failed", "cancelled") else time.time())
        - created_at,
        2,
    )

    budget_info = BudgetInfo(
        tokens_used=tokens_in + tokens_out,
        tokens_max=150000,
        llm_calls=llm_calls,
        searches=searches,
        elapsed_s=max(0.0, elapsed_s),
    )

    # Report
    report_data = None
    report_raw = r.get(key_run_report(run_id))
    if report_raw:
        report_json = json.loads(_decode(report_raw))

        # Build claims
        claims_list: List[ClaimData] = []
        for section in report_json.get("sections", []):
            for c in section.get("claims", []):
                claims_list.append(
                    ClaimData(
                        text=c.get("text", ""),
                        citations=c.get("finding_ids", []),
                    )
                )

        # Build sources from findings hash
        findings_raw = r.hgetall(key_run_findings(run_id))
        sources_list: List[SourceData] = []
        for fid_b, fjson_b in findings_raw.items():
            f_obj = json.loads(_decode(fjson_b))
            sources_list.append(
                SourceData(
                    label=f_obj.get("finding_id", _decode(fid_b)),
                    url=f_obj.get("source_url", ""),
                    title=f_obj.get("title"),
                    retrieved_at=f_obj.get("retrieved_at"),
                )
            )

        report_data = ReportData(
            markdown=report_json.get("markdown", ""),
            claims=claims_list,
            sources=sources_list,
            coverage_gaps=report_json.get("coverage_gaps", []),
            claim_support_status="not_evaluated",
        )

    return ResearchStatusResponse(
        run_id=run_id,
        status=status,
        question=question,
        progress=ProgressInfo(subquestions_total=total_sq, subquestions_done=done_sq),
        budget=budget_info,
        report=report_data,
        termination_reason=termination_reason if termination_reason else None,
        termination_detail=meta.get("termination_detail") or None,
    )


@router.get("/{run_id}/events")
async def stream_research_events(
    run_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    last_id_param: Optional[str] = Query(None, alias="last_event_id"),
) -> EventSourceResponse:
    """Streams progress events via Server-Sent Events (SSE) with Last-Event-ID replay (PRD FR-5.2)."""
    r = get_redis_client()
    meta_raw = r.hgetall(key_run_meta(run_id))
    if not meta_raw:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Research run '{run_id}' not found"},
        )

    effective_last_id = last_event_id or last_id_param
    stream_k = key_run_events(run_id)

    async def event_generator() -> AsyncGenerator[Dict[str, Any], None]:
        last_seen_id = "0-0"

        # 1. Replay missed historical events
        if effective_last_id:
            # Exclusive range query from effective_last_id to end of current stream
            min_bound = f"({effective_last_id}"
            try:
                historical = r.xrange(stream_k, min=min_bound, max="+")
            except Exception:
                historical = r.xrange(stream_k, min="-", max="+")
        else:
            historical = r.xrange(stream_k, min="-", max="+")

        for entry in historical:
            msg_id = _decode(entry[0])
            fields = {_decode(k): _decode(v) for k, v in entry[1].items()}
            event_type = fields.get("event", "progress")
            last_seen_id = msg_id
            yield {
                "id": msg_id,
                "event": event_type,
                "data": json.dumps(fields),
            }

        # 2. Live streaming loop
        idle_counter = 0
        while True:
            if await request.is_disconnected():
                logger.info("Client disconnected from SSE stream for run '%s'", run_id)
                break

            # Read new entries from stream
            entries = r.xread({stream_k: last_seen_id}, count=50, block=1000)
            if entries:
                idle_counter = 0
                for _, stream_entries in entries:
                    for entry in stream_entries:
                        msg_id = _decode(entry[0])
                        fields = {_decode(k): _decode(v) for k, v in entry[1].items()}
                        event_type = fields.get("event", "progress")
                        last_seen_id = msg_id
                        yield {
                            "id": msg_id,
                            "event": event_type,
                            "data": json.dumps(fields),
                        }
            else:
                idle_counter += 1
                if idle_counter >= 15:  # 15s idle heartbeat
                    idle_counter = 0
                    yield {
                        "event": "heartbeat",
                        "data": json.dumps({"timestamp": str(time.time()), "run_id": run_id}),
                    }

            # Check if run reached a terminal state
            current_status = _decode(r.hget(key_run_meta(run_id), "status") or "")
            if current_status in ("completed", "completed_partial", "failed", "cancelled"):
                # Ensure no pending unread stream entries remain
                pending = r.xread({stream_k: last_seen_id}, count=10, block=100)
                if not pending:
                    break

            await asyncio.sleep(0.2)

    return EventSourceResponse(event_generator())


@router.post("/{run_id}/cancel", status_code=202, response_model=ActionResponse)
def cancel_research_run(run_id: str) -> ActionResponse:
    """Requests cooperative cancellation of an active or queued research run (PRD §8)."""
    r = get_redis_client()
    meta_raw = r.hgetall(key_run_meta(run_id))
    if not meta_raw:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Research run '{run_id}' not found"},
        )

    # Flag cancellation in metadata
    r.hset(key_run_meta(run_id), "cancellation_requested", "1")

    # Emit cancellation event
    r.xadd(
        key_run_events(run_id),
        {"event": "run_cancel_requested", "run_id": run_id, "timestamp": str(time.time())},
    )

    logger.info("Cancellation requested for run '%s'", run_id)
    return ActionResponse(run_id=run_id, status="cancel_requested", message="Cancellation request recorded")


@router.post("/{run_id}/resume", status_code=202, response_model=ActionResponse)
def resume_research_run(run_id: str) -> ActionResponse:
    """Recovers a stalled, quota-limited, or worker-lost research run (PRD §8, FR-4.4)."""
    r = get_redis_client()
    meta_raw = r.hgetall(key_run_meta(run_id))
    if not meta_raw:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Research run '{run_id}' not found"},
        )

    meta = {_decode(k): _decode(v) for k, v in meta_raw.items()}
    status = meta.get("status", "")
    reason = meta.get("termination_reason", "")

    # Only failed runs with a recoverable reason may resume; other outcomes are immutable (PRD FR-4.4).
    if status != "failed" or reason not in RECOVERABLE_REASONS:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "not_recoverable",
                "message": f"Run '{run_id}' is '{status}' (reason: '{reason}'); only failed runs with "
                f"reason in {sorted(RECOVERABLE_REASONS)} can be resumed",
            },
        )

    # Re-queue to queue:runs
    r.rpush(key_queue_runs(), run_id)
    r.hset(key_run_meta(run_id), "status", "queued")

    # Emit resumed event
    r.xadd(
        key_run_events(run_id),
        {"event": "run_resumed", "run_id": run_id, "previous_status": status, "timestamp": str(time.time())},
    )

    logger.info("Resumed research run '%s' (previous status: %s)", run_id, status)
    return ActionResponse(run_id=run_id, status="resumed", message=f"Run '{run_id}' re-enqueued for worker pickup")


@router.get("/{run_id}/trace", response_model=ResearchTraceResponse)
def get_research_trace(
    run_id: str,
    include: Optional[str] = Query(None, description="Set to 'payloads' to inline payloads"),
    include_payloads: bool = Query(False, description="Alternative boolean flag for inlining payloads"),
) -> ResearchTraceResponse:
    """Returns the ordered, redacted step-level execution trace for a research run (PRD §8, FR-5.5)."""
    r = get_redis_client()
    meta_raw = r.hgetall(key_run_meta(run_id))
    if not meta_raw:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_found", "message": f"Research run '{run_id}' not found"},
        )

    should_include = (include == "payloads") or include_payloads
    trace_store = TraceStore(r)
    steps = trace_store.get_trace(run_id, include_payloads=should_include)

    return ResearchTraceResponse(
        run_id=run_id,
        total_steps=len(steps),
        steps=steps,
    )

