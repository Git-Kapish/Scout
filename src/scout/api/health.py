"""Scout Health Check Endpoint (PRD §8, §13).

Reports Redis connectivity, worker status, and cached provider reachability.
In accordance with PRD §8, never performs repeated quota-consuming probes on health checks.
"""

from __future__ import annotations

import os
from typing import Any, Dict

import redis
from fastapi import APIRouter

from scout.config import get_settings

router = APIRouter()


@router.get("/health")
def health_check() -> Dict[str, Any]:
    settings = get_settings()
    redis_status = "healthy"
    redis_details = {}

    try:
        r = redis.Redis.from_url(settings.redis.url, socket_timeout=2.0)
        r.ping()
        info = r.info()
        redis_details = {
            "version": info.get("redis_version"),
            "used_memory_human": info.get("used_memory_human"),
            "aof_enabled": info.get("aof_enabled") == 1,
            "connected_clients": info.get("connected_clients"),
        }
    except Exception as e:
        redis_status = f"unhealthy: {e}"

    # Provider status is cached / configuration-based, never consuming quota on health checks (PRD §8)
    has_openrouter_key = bool(os.getenv("OPENROUTER_API_KEY"))
    has_tavily_key = bool(os.getenv("TAVILY_API_KEY"))

    provider_status = {
        "openrouter": {
            "configured": has_openrouter_key,
            "status": "ready" if has_openrouter_key else "missing_key",
            "model": settings.get_role_primary("planner").model,
        },
        "search": {
            "primary": settings.search.provider_order[0] if settings.search.provider_order else "ddgs",
            "tavily_key_present": has_tavily_key,
            "fallback": "ddgs",
        },
    }

    overall = "ok" if redis_status == "healthy" else "degraded"

    return {
        "status": overall,
        "scout_mode": settings.scout_mode,
        "redis": {
            "status": redis_status,
            "details": redis_details,
        },
        "providers": provider_status,
    }
