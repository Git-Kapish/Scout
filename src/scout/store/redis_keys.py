"""Redis Keyspace Constants and Formatters (PRD §9.2).

Centralizes all Redis keyspace conventions to prevent key drift:
- queue:runs
- runs:active
- lease:{run_id}
- run:{id}:meta
- run:{id}:budget
- run:{id}:committed
- run:{id}:generation
- idem:{key}
- run:{id}:effects
- run:{id}:findings
- run:{id}:report
- run:{id}:events
- run:{id}:trace
- provider:{name}:{utc_date}:requests
- provider:{name}:{utc_date}:tokens
"""

from __future__ import annotations

from datetime import datetime, timezone


def key_queue_runs() -> str:
    return "queue:runs"


def key_runs_active() -> str:
    return "runs:active"


def key_lease(run_id: str) -> str:
    return f"lease:{run_id}"


def key_run_meta(run_id: str) -> str:
    return f"run:{run_id}:meta"


def key_run_budget(run_id: str) -> str:
    return f"run:{run_id}:budget"


def key_run_committed(run_id: str) -> str:
    return f"run:{run_id}:committed"


def key_run_generation(run_id: str) -> str:
    return f"run:{run_id}:generation"


def key_idempotency(idem_key: str) -> str:
    return f"idem:{idem_key}"


def key_run_effects(run_id: str) -> str:
    return f"run:{run_id}:effects"


def key_run_findings(run_id: str) -> str:
    return f"run:{run_id}:findings"


def key_run_report(run_id: str) -> str:
    return f"run:{run_id}:report"


def key_run_events(run_id: str) -> str:
    return f"run:{run_id}:events"


def key_run_trace(run_id: str) -> str:
    return f"run:{run_id}:trace"


def key_provider_daily_requests(provider: str, utc_date: str | None = None) -> str:
    date_str = utc_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"provider:{provider}:{date_str}:requests"


def key_provider_daily_tokens(provider: str, utc_date: str | None = None) -> str:
    date_str = utc_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"provider:{provider}:{date_str}:tokens"
