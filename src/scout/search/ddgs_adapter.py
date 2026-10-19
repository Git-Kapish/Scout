"""DuckDuckGo Search Adapter (PRD §5.4 fallback adapter)."""

from __future__ import annotations

import logging
from typing import List

from duckduckgo_search import DDGS
from duckduckgo_search.exceptions import RatelimitException

from scout.search.base import (
    SearchHit,
    SearchProvider,
    SearchResult,
    filter_and_deduplicate_hits,
)
from scout.search.canonical import extract_domain

logger = logging.getLogger(__name__)


class DuckDuckGoSearchAdapter(SearchProvider):
    @property
    def name(self) -> str:
        return "ddgs"

    def search(
        self,
        query: str,
        max_results: int = 5,
        max_per_domain: int = 2,
    ) -> SearchResult:
        try:
            # Over-fetch slightly to allow for domain deduplication and filtering
            fetch_count = max_results * 2
            raw_results: List[dict] = []
            with DDGS() as ddgs:
                raw_results = list(ddgs.text(query, max_results=fetch_count))

            if not raw_results:
                return SearchResult(
                    query=query,
                    status="no_results",
                    results=[],
                    provider=self.name,
                    credits_used=0.0,
                )

            hits = [
                SearchHit(
                    title=r.get("title", ""),
                    url=r.get("href", ""),
                    snippet=r.get("body", ""),
                    domain=extract_domain(r.get("href", "")),
                )
                for r in raw_results
                if r.get("href")
            ]

            filtered = filter_and_deduplicate_hits(
                hits, max_results=max_results, max_per_domain=max_per_domain
            )

            if not filtered:
                return SearchResult(
                    query=query,
                    status="no_results",
                    results=[],
                    provider=self.name,
                    credits_used=0.0,
                )

            return SearchResult(
                query=query,
                status="ok",
                results=filtered,
                provider=self.name,
                credits_used=0.0,
            )

        except RatelimitException as re:
            logger.warning("DuckDuckGo rate limited: %s", re)
            return SearchResult(
                query=query,
                status="rate_limited",
                results=[],
                provider=self.name,
                credits_used=0.0,
                retry_after=60,
                error_message=str(re),
            )
        except Exception as e:
            logger.error("DuckDuckGo search error: %s", e)
            return SearchResult(
                query=query,
                status="error",
                results=[],
                provider=self.name,
                credits_used=0.0,
                error_message=str(e),
            )
