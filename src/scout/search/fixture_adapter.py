"""Fixture Search Adapter for deterministic replay and tests (PRD §5.4)."""

from __future__ import annotations

from typing import Dict, List, Optional

from scout.search.base import (
    SearchHit,
    SearchProvider,
    SearchResult,
    filter_and_deduplicate_hits,
)


class FixtureSearchAdapter(SearchProvider):
    def __init__(self, fixtures: Optional[Dict[str, List[dict]]] = None):
        self.fixtures = fixtures or {}

    @property
    def name(self) -> str:
        return "fixture"

    def register_fixture(self, query: str, hits: List[dict]) -> None:
        self.fixtures[query.lower().strip()] = hits

    def search(
        self,
        query: str,
        max_results: int = 5,
        max_per_domain: int = 2,
    ) -> SearchResult:
        key = query.lower().strip()
        raw_hits = self.fixtures.get(key, [])
        if not raw_hits:
            # Check for partial match
            for k, v in self.fixtures.items():
                if k in key or key in k:
                    raw_hits = v
                    break

        hits = [
            SearchHit(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", ""),
                domain=r.get("domain", ""),
            )
            for r in raw_hits
        ]

        filtered = filter_and_deduplicate_hits(
            hits, max_results=max_results, max_per_domain=max_per_domain
        )

        return SearchResult(
            query=query,
            status="ok" if filtered else "no_results",
            results=filtered,
            provider=self.name,
            credits_used=0.0,
        )
