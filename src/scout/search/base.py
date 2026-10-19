"""Search Provider Interfaces and Data Models (PRD FR-3.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from scout.search.canonical import canonicalize_url, extract_domain

SearchStatus = Literal[
    "ok",
    "partial",
    "no_results",
    "rate_limited",
    "timeout",
    "error",
]


class SearchHit(BaseModel):
    title: str
    url: str
    snippet: str
    domain: str
    published_date: Optional[str] = None
    score: Optional[float] = None


class SearchResult(BaseModel):
    query: str
    status: SearchStatus
    results: List[SearchHit] = Field(default_factory=list)
    provider: str
    credits_used: float = 0.0
    retry_after: Optional[int] = None
    error_message: Optional[str] = None


def filter_and_deduplicate_hits(
    hits: List[SearchHit],
    max_results: int = 5,
    max_per_domain: int = 2,
) -> List[SearchHit]:
    """Canonicalizes URLs, deduplicates, and caps results per domain (PRD FR-3.1)."""
    seen_urls = set()
    domain_counts: dict[str, int] = {}
    filtered: List[SearchHit] = []

    for hit in hits:
        c_url = canonicalize_url(hit.url)
        if not c_url or c_url in seen_urls:
            continue

        domain = extract_domain(c_url)
        count = domain_counts.get(domain, 0)
        if count >= max_per_domain:
            continue

        seen_urls.add(c_url)
        domain_counts[domain] = count + 1

        hit.url = c_url
        hit.domain = domain
        filtered.append(hit)

        if len(filtered) >= max_results:
            break

    return filtered


class SearchProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        """Name of the provider (e.g. 'tavily', 'ddgs')."""
        pass

    @abstractmethod
    def search(
        self,
        query: str,
        max_results: int = 5,
        max_per_domain: int = 2,
    ) -> SearchResult:
        """Executes a search query with deduplication and domain caps."""
        pass
