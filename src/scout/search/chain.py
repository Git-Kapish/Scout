"""Search Provider Chain with Fallback Routing (PRD FR-3.1, §5.4)."""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from scout.config import SearchConfig, get_settings
from scout.search.base import SearchProvider, SearchResult
from scout.search.ddgs_adapter import DuckDuckGoSearchAdapter
from scout.search.fixture_adapter import FixtureSearchAdapter
from scout.search.tavily_adapter import TavilySearchAdapter

logger = logging.getLogger(__name__)


class SearchChain:
    def __init__(
        self,
        providers: Optional[List[SearchProvider]] = None,
        search_config: Optional[SearchConfig] = None,
    ):
        self.config = search_config or get_settings().search
        if providers:
            self.providers = providers
        else:
            self.providers = self._init_default_providers()

    def _init_default_providers(self) -> List[SearchProvider]:
        registry: Dict[str, SearchProvider] = {
            "tavily": TavilySearchAdapter(),
            "ddgs": DuckDuckGoSearchAdapter(),
            "fixture": FixtureSearchAdapter(),
        }
        chain: List[SearchProvider] = []
        for name in self.config.provider_order:
            if name in registry:
                chain.append(registry[name])
        if not chain:
            chain.append(DuckDuckGoSearchAdapter())
        return chain

    def search(
        self,
        query: str,
        max_results: Optional[int] = None,
        max_per_domain: Optional[int] = None,
    ) -> SearchResult:
        limit = max_results or self.config.max_results_per_query
        domain_cap = max_per_domain or self.config.max_results_per_domain

        last_result: Optional[SearchResult] = None
        for provider in self.providers:
            try:
                res = provider.search(query, max_results=limit, max_per_domain=domain_cap)
                # Successful or clean empty result: return directly
                if res.status in ("ok", "no_results"):
                    return res

                # On rate_limited, timeout, or error: log and try next provider (PRD FR-3.1, FR-3.4)
                logger.warning(
                    "Search provider '%s' returned status '%s': %s. Trying fallback.",
                    provider.name,
                    res.status,
                    res.error_message,
                )
                last_result = res
            except Exception as e:
                logger.error("Search provider '%s' raised exception: %s", provider.name, e)
                last_result = SearchResult(
                    query=query,
                    status="error",
                    results=[],
                    provider=provider.name,
                    credits_used=0.0,
                    error_message=str(e),
                )

        return last_result or SearchResult(
            query=query,
            status="error",
            results=[],
            provider="none",
            credits_used=0.0,
            error_message="All search providers exhausted",
        )
