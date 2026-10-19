"""Tavily Search Adapter (PRD §5.1, §5.4).

Primary search adapter using Tavily basic search (1 credit per query).
Explicitly never invokes Tavily Extract to preserve monthly quota.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from tavily import InvalidAPIKeyError, TavilyClient, UsageLimitExceededError

from scout.search.base import (
    SearchHit,
    SearchProvider,
    SearchResult,
    filter_and_deduplicate_hits,
)
from scout.search.canonical import extract_domain

logger = logging.getLogger(__name__)


class TavilySearchAdapter(SearchProvider):
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("TAVILY_API_KEY")
        self._client: Optional[TavilyClient] = None
        if self.api_key:
            self._client = TavilyClient(api_key=self.api_key)

    @property
    def name(self) -> str:
        return "tavily"

    def search(
        self,
        query: str,
        max_results: int = 5,
        max_per_domain: int = 2,
    ) -> SearchResult:
        if not self._client:
            return SearchResult(
                query=query,
                status="error",
                results=[],
                provider=self.name,
                credits_used=0.0,
                error_message="TAVILY_API_KEY is not set",
            )

        try:
            # PRD §5.1: Basic search depth strictly 1 credit, never advanced or extract
            response = self._client.search(
                query=query,
                search_depth="basic",
                max_results=max_results * 2,
                include_answer=False,
                include_raw_content=False,
            )

            raw_results = response.get("results", [])
            if not raw_results:
                return SearchResult(
                    query=query,
                    status="no_results",
                    results=[],
                    provider=self.name,
                    credits_used=1.0,
                )

            hits = [
                SearchHit(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    snippet=r.get("content", ""),
                    domain=extract_domain(r.get("url", "")),
                    score=r.get("score"),
                )
                for r in raw_results
                if r.get("url")
            ]

            filtered = filter_and_deduplicate_hits(
                hits, max_results=max_results, max_per_domain=max_per_domain
            )

            return SearchResult(
                query=query,
                status="ok" if filtered else "no_results",
                results=filtered,
                provider=self.name,
                credits_used=1.0,
            )

        except UsageLimitExceededError as ue:
            logger.warning("Tavily usage/credit limit exceeded: %s", ue)
            return SearchResult(
                query=query,
                status="rate_limited",
                results=[],
                provider=self.name,
                credits_used=0.0,
                error_message=str(ue),
            )
        except InvalidAPIKeyError as ie:
            logger.error("Tavily invalid API key: %s", ie)
            return SearchResult(
                query=query,
                status="error",
                results=[],
                provider=self.name,
                credits_used=0.0,
                error_message=str(ie),
            )
        except Exception as e:
            logger.error("Tavily search error: %s", e)
            return SearchResult(
                query=query,
                status="error",
                results=[],
                provider=self.name,
                credits_used=0.0,
                error_message=str(e),
            )
