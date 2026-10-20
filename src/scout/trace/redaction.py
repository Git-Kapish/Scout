"""Payload Redaction and Preview Helpers for Step Trace (PRD FR-5.5, §10).

Redacts:
- Secrets, API keys, Bearer tokens
- Authorization and Cookie headers
- Sensitive URL query parameters (api_key, token, key, secret, auth)
- Replaces large page text with hash references and 2 KB previews
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from scout.trace.models import TracePayload

# Regular expressions for sensitive tokens and keys
SECRET_PATTERNS = [
    re.compile(r"(sk-[a-zA-Z0-9_\-]{16,})", re.IGNORECASE),
    re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{16,}", re.IGNORECASE),
    re.compile(r"(api[_\-]?key\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{8,}(['\"]?)", re.IGNORECASE),
    re.compile(r"(token\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{8,}(['\"]?)", re.IGNORECASE),
    re.compile(r"(secret\s*[:=]\s*['\"]?)[a-zA-Z0-9_\-]{8,}(['\"]?)", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*['\"]?)[^\s,'\"]+(['\"]?)", re.IGNORECASE),
]

SENSITIVE_QUERY_PARAMS = {
    "api_key",
    "apikey",
    "key",
    "token",
    "access_token",
    "auth",
    "secret",
    "password",
    "client_secret",
}

SENSITIVE_HEADER_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "openrouter_api_key",
    "groq_api_key",
    "tavily_api_key",
}

MAX_PREVIEW_BYTES = 2048
MAX_PAYLOAD_BYTES = 65536  # 64 KB inline cap for ?include=payloads


def sanitize_url(url: str) -> str:
    """Strips sensitive query parameters from URLs."""
    if not url or not isinstance(url, str):
        return str(url)
    try:
        parsed = urlparse(url)
        if not parsed.query:
            return url
        params = parse_qs(parsed.query, keep_blank_values=True)
        sanitized_params = {}
        for k, v in params.items():
            if k.lower() in SENSITIVE_QUERY_PARAMS:
                sanitized_params[k] = ["[REDACTED]"]
            else:
                sanitized_params[k] = v
        new_query = urlencode(sanitized_params, doseq=True, safe="[]")
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        ))
    except Exception:
        return url


def redact_string(val: str) -> str:
    """Applies regex redaction to strings."""
    if not val or not isinstance(val, str):
        return val

    # Sanitize embedded URLs
    if "http://" in val or "https://" in val:
        val = re.sub(
            r"https?://[^\s\"'>]+",
            lambda m: sanitize_url(m.group(0)),
            val,
        )

    # Sanitize secrets
    for pat in SECRET_PATTERNS:
        val = pat.sub(r"\1[REDACTED]\2" if r"\2" in pat.pattern else r"[REDACTED]", val)

    return val


def redact_payload(obj: Any) -> Any:
    """Recursively redacts sensitive keys, headers, query params, and values."""
    if isinstance(obj, str):
        return redact_string(obj)
    elif isinstance(obj, dict):
        redacted = {}
        for k, v in obj.items():
            key_lower = str(k).lower()
            if key_lower in SENSITIVE_HEADER_KEYS or key_lower in SENSITIVE_QUERY_PARAMS:
                redacted[k] = "[REDACTED]"
            elif isinstance(v, (str, dict, list)):
                redacted[k] = redact_payload(v)
            else:
                redacted[k] = v
        return redacted
    elif isinstance(obj, (list, tuple)):
        return [redact_payload(item) for item in obj]
    elif hasattr(obj, "model_dump"):
        return redact_payload(obj.model_dump())
    return obj


def create_trace_payload(
    raw_data: Any,
    include_full: bool = False,
    reference_override: Optional[str] = None,
) -> TracePayload:
    """Constructs a TracePayload with reference, 2 KB preview, and optional redacted full body."""
    if raw_data is None:
        return TracePayload(reference=None, preview="", full=None)

    # Extract dict / json representation
    if hasattr(raw_data, "model_dump"):
        data_obj = raw_data.model_dump()
    elif isinstance(raw_data, (dict, list, str, int, float, bool)):
        data_obj = raw_data
    else:
        data_obj = str(raw_data)

    # Redact before preview or inlining
    redacted_data = redact_payload(data_obj)

    # Convert to serialized string for preview and size checking
    if isinstance(redacted_data, str):
        serialized = redacted_data
    else:
        serialized = json.dumps(redacted_data, default=str, ensure_ascii=False)

    ref = reference_override
    # Large content detection: compute hash reference if content is large
    encoded_bytes = serialized.encode("utf-8")
    if len(encoded_bytes) > MAX_PREVIEW_BYTES and not ref:
        content_hash = hashlib.sha256(encoded_bytes).hexdigest()
        ref = f"hash:{content_hash}"

    # Generate 2 KB preview
    if len(encoded_bytes) > MAX_PREVIEW_BYTES:
        preview_text = serialized[:MAX_PREVIEW_BYTES] + f"... [truncated, total {len(encoded_bytes)} bytes]"
    else:
        preview_text = serialized

    # Full payload inclusion if requested and under cap
    full_data = None
    if include_full:
        if len(encoded_bytes) <= MAX_PAYLOAD_BYTES:
            full_data = redacted_data
        else:
            full_data = {
                "_truncated": True,
                "_original_bytes": len(encoded_bytes),
                "_preview": preview_text,
                "reference": ref,
            }

    return TracePayload(
        reference=ref,
        preview=preview_text,
        full=full_data,
    )
