"""Web Page Fetcher and Main Text Extractor (PRD FR-3.2, §10).

Features:
- SSRF guard re-checked on every HTTP redirect
- Strict response size cap (max 2 MB) and timeout
- Trafilatura main content extraction
- Heuristic paywall / block detection
- Content-addressed disk caching (data/cache/pages/{sha256}.txt)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import httpx
import trafilatura
from pydantic import BaseModel

from scout.config import FetchConfig, find_project_root, get_settings
from scout.fetch.ssrf import validate_url_safe
from scout.schemas import ResearchStatus

logger = logging.getLogger(__name__)

PAYWALL_MARKERS = [
    "paywall",
    "subscribe to read",
    "subscription required",
    "article is for subscribers",
    "become a member",
    "turn off your ad blocker",
    "access denied",
    "security check to continue",
    "cloudflare challenge",
]


class PageFetchResult(BaseModel):
    url: str
    status: ResearchStatus
    text_content: str = ""
    page_hash: str = ""
    status_code: Optional[int] = None
    error_message: Optional[str] = None


class PageFetcher:
    def __init__(self, config: Optional[FetchConfig] = None, cache_dir: Optional[Path] = None):
        self.config = config or get_settings().fetch
        if cache_dir is None:
            self.cache_dir = find_project_root() / "data" / "cache" / "pages"
        else:
            self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def fetch(self, url: str) -> PageFetchResult:
        # Step 1: Initial SSRF check (PRD §10)
        is_safe, reason = validate_url_safe(url)
        if not is_safe:
            logger.warning("SSRF blocked URL '%s': %s", url, reason)
            return PageFetchResult(
                url=url,
                status="blocked",
                error_message=f"SSRF guard blocked destination: {reason}",
            )

        current_url = url
        redirect_count = 0
        max_redirects = self.config.max_redirects
        headers = {"User-Agent": self.config.user_agent}

        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.config.timeout_s, connect=5.0),
                headers=headers,
                follow_redirects=False,
            ) as client:
                while redirect_count <= max_redirects:
                    try:
                        # Stream response to strictly enforce byte ceiling
                        with client.stream("GET", current_url) as response:
                            status_code = response.status_code

                            # Handle Redirects manually to re-verify SSRF
                            if status_code in (301, 302, 303, 307, 308):
                                redirect_target = response.headers.get("location")
                                if not redirect_target:
                                    return PageFetchResult(
                                        url=current_url,
                                        status="error",
                                        status_code=status_code,
                                        error_message="Redirect missing Location header",
                                    )
                                # Resolve relative redirect
                                next_url = urljoin(current_url, redirect_target)
                                redirect_count += 1
                                if redirect_count > max_redirects:
                                    return PageFetchResult(
                                        url=current_url,
                                        status="error",
                                        status_code=status_code,
                                        error_message=f"Exceeded max redirects ({max_redirects})",
                                    )

                                # Re-validate destination with SSRF guard (PRD §10)
                                is_safe, reason = validate_url_safe(next_url)
                                if not is_safe:
                                    logger.warning("SSRF blocked redirect to '%s': %s", next_url, reason)
                                    return PageFetchResult(
                                        url=next_url,
                                        status="blocked",
                                        error_message=f"SSRF blocked redirect: {reason}",
                                    )
                                current_url = next_url
                                continue

                            # Handle Status Code errors
                            if status_code in (401, 402, 403):
                                return PageFetchResult(
                                    url=current_url,
                                    status="paywalled" if status_code == 402 else "blocked",
                                    status_code=status_code,
                                    error_message=f"HTTP status {status_code}",
                                )

                            if status_code >= 400:
                                return PageFetchResult(
                                    url=current_url,
                                    status="error",
                                    status_code=status_code,
                                    error_message=f"HTTP status {status_code}",
                                )

                            # Verify Content-Type
                            content_type = response.headers.get("content-type", "").lower()
                            if content_type and not any(
                                t in content_type for t in ("text/", "application/xhtml+xml", "application/xml")
                            ):
                                return PageFetchResult(
                                    url=current_url,
                                    status="blocked",
                                    status_code=status_code,
                                    error_message=f"Unsupported non-text content type: {content_type}",
                                )

                            # Read stream with size limit
                            chunks = []
                            total_bytes = 0
                            for chunk in response.iter_bytes():
                                total_bytes += len(chunk)
                                if total_bytes > self.config.max_size_bytes:
                                    return PageFetchResult(
                                        url=current_url,
                                        status="blocked",
                                        status_code=status_code,
                                        error_message=f"Response exceeded size cap ({self.config.max_size_bytes} bytes)",
                                    )
                                chunks.append(chunk)

                            raw_bytes = b"".join(chunks)
                            raw_html = raw_bytes.decode("utf-8", errors="replace")
                            break

                    except httpx.TimeoutException as te:
                        return PageFetchResult(
                            url=current_url,
                            status="timeout",
                            error_message=f"Request timed out: {te}",
                        )
                    except httpx.RequestError as re:
                        return PageFetchResult(
                            url=current_url,
                            status="error",
                            error_message=f"Request network error: {re}",
                        )

            # Step 2: Main Text Extraction via Trafilatura
            extracted_text = trafilatura.extract(
                raw_html,
                include_comments=False,
                include_tables=True,
                no_fallback=False,
            ) or ""

            extracted_text = extracted_text.strip()
            page_hash = hashlib.sha256(raw_bytes).hexdigest()

            # Step 3: Heuristic Paywall & Block Detection
            lower_text = extracted_text.lower()
            if len(extracted_text) < 150 and any(m in lower_text for m in PAYWALL_MARKERS):
                return PageFetchResult(
                    url=current_url,
                    status="paywalled",
                    text_content=extracted_text,
                    page_hash=page_hash,
                    status_code=status_code,
                    error_message="Detected paywall or subscription gate",
                )

            if len(extracted_text) < 50:
                return PageFetchResult(
                    url=current_url,
                    status="no_results",
                    text_content=extracted_text,
                    page_hash=page_hash,
                    status_code=status_code,
                    error_message="Extracted text is empty or too short",
                )

            # Step 4: Persist page text to disk cache (PRD FR-4.5)
            disk_cache_file = self.cache_dir / f"{page_hash}.txt"
            if not disk_cache_file.exists():
                with open(disk_cache_file, "w", encoding="utf-8") as f:
                    f.write(extracted_text)

            return PageFetchResult(
                url=current_url,
                status="ok",
                text_content=extracted_text,
                page_hash=page_hash,
                status_code=status_code,
            )

        except Exception as e:
            logger.error("Unexpected error fetching '%s': %s", url, e)
            return PageFetchResult(
                url=url,
                status="error",
                error_message=str(e),
            )
