from scout.search.base import (
    SearchHit,
    SearchProvider,
    SearchResult,
    SearchStatus,
    filter_and_deduplicate_hits,
)
from scout.search.canonical import canonicalize_url, extract_domain
from scout.search.chain import SearchChain
from scout.search.ddgs_adapter import DuckDuckGoSearchAdapter
from scout.search.fixture_adapter import FixtureSearchAdapter
from scout.search.tavily_adapter import TavilySearchAdapter

__all__ = [
    "SearchHit",
    "SearchProvider",
    "SearchResult",
    "SearchStatus",
    "filter_and_deduplicate_hits",
    "canonicalize_url",
    "extract_domain",
    "SearchChain",
    "DuckDuckGoSearchAdapter",
    "FixtureSearchAdapter",
    "TavilySearchAdapter",
]
