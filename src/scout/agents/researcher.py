"""Researcher Agent (PRD FR-1.3, FR-3.1 - FR-3.4).

Executes a single SubQuestion deterministically under budget:
Query Generation -> Search -> SSRF-guarded Fetch -> BM25 Selection -> Finding Extraction -> Provenance Check.
"""

from __future__ import annotations

import logging
import uuid
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from scout.config import get_settings
from scout.effects.fault_injection import maybe_crash
from scout.fetch.fetcher import PageFetcher
from scout.fetch.passage import select_top_passages_bm25, split_into_paragraphs
from scout.llm import LLMClient
from scout.schemas import Finding, ResearchResult, SubQuestion, ToolFailure
from scout.search import SearchChain
from scout.validate.provenance import verify_snippet_in_page

logger = logging.getLogger(__name__)


class QueryGenerationOutput(BaseModel):
    queries: List[str] = Field(
        min_length=1,
        max_length=3,
        description="1 to 3 targeted web search queries",
    )


class CandidateFinding(BaseModel):
    claim: str = Field(description="Factual claim addressing the research sub-question")
    snippet: str = Field(description="Exact verbatim quote from the provided text supporting the claim")


class ExtractionOutput(BaseModel):
    findings: List[CandidateFinding] = Field(default_factory=list)


QUERY_GEN_PROMPT = """You are a precision web search query generator.
Your goal is to write 1 to 3 targeted search queries to find authoritative information for this sub-question.

Sub-Question: {question}
Rationale: {rationale}
Needs Recency: {needs_recency}

Rules:
1. Output 1 to 3 distinct queries.
2. Use precise keywords, technical terms, and entities. Avoid conversational phrases like "can you tell me".
3. If recency is needed, include current years/dates.
4. Output ONLY valid JSON matching the schema.
"""

EXTRACTION_PROMPT = """You are a factual evidence extraction specialist.
Your goal is to extract key findings from the provided text passages that directly answer the sub-question.

Sub-Question: {question}

PASSAGES:
{passages}

CRITICAL RULES:
1. Every finding must have a 'claim' (your summary of the fact) and a 'snippet'.
2. The 'snippet' MUST BE A STRICTLY VERBATIM, EXACT QUOTE copied word-for-word from the passages above.
   DO NOT alter or paraphrase the snippet. Any alteration will fail mechanical validation.
3. If the passages contain no relevant facts, return an empty findings list.
4. Output ONLY valid JSON matching the schema.
"""


class ResearcherAgent:
    def __init__(
        self,
        llm_client: Optional[LLMClient] = None,
        search_chain: Optional[SearchChain] = None,
        fetcher: Optional[PageFetcher] = None,
    ):
        self.llm_client = llm_client or LLMClient()
        self.search_chain = search_chain or SearchChain()
        self.fetcher = fetcher or PageFetcher()
        self.settings = get_settings()

    def generate_queries(
        self,
        sub_question: SubQuestion,
        remaining_budget: Optional[int] = None,
    ) -> List[str]:
        system_msg = QUERY_GEN_PROMPT.format(
            question=sub_question.question,
            rationale=sub_question.rationale,
            needs_recency=sub_question.needs_recency,
        )
        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": f"Generate search queries for: {sub_question.question}"},
        ]
        resp = self.llm_client.generate(
            role="researcher_query",
            messages=messages,
            response_model=QueryGenerationOutput,
            remaining_token_budget=remaining_budget,
        )
        parsed: QueryGenerationOutput = resp.parsed
        return parsed.queries

    def extract_findings_from_passages(
        self,
        sub_question: SubQuestion,
        passages: List[str],
        page_text: str,
        source_url: str,
        domain: str,
        page_hash: str,
        title: Optional[str] = None,
        remaining_budget: Optional[int] = None,
        max_retries: int = 1,
    ) -> Tuple[List[Finding], int]:
        """Extracts candidate findings and validates snippet provenance verbatim."""
        passages_text = "\n\n---\n\n".join(passages)
        prompt = EXTRACTION_PROMPT.format(
            question=sub_question.question,
            passages=passages_text,
        )
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": "Extract all supported findings."},
        ]

        attempt = 0
        verified_findings: List[Finding] = []
        dropped_count = 0

        while attempt <= max_retries:
            resp = self.llm_client.generate(
                role="researcher_extract",
                messages=messages,
                response_model=ExtractionOutput,
                remaining_token_budget=remaining_budget,
            )
            parsed: ExtractionOutput = resp.parsed

            unverified_candidates = []
            for candidate in parsed.findings:
                # Mechanical Snippet Verification (PRD §4.3 #3, FR-3.3)
                is_valid = verify_snippet_in_page(candidate.snippet, page_text)
                if is_valid:
                    # Deduplicate against already accepted findings from this page
                    already_exists = any(
                        f.snippet == candidate.snippet.strip() or f.claim == candidate.claim.strip()
                        for f in verified_findings
                    )
                    if not already_exists:
                        f_id = f"find_{uuid.uuid4().hex[:8]}"
                        verified_findings.append(
                            Finding(
                                finding_id=f_id,
                                subquestion_id=sub_question.id,
                                claim=candidate.claim.strip(),
                                snippet=candidate.snippet.strip(),
                                source_url=source_url,
                                domain=domain,
                                title=title,
                                page_hash=page_hash,
                                verified=True,
                            )
                        )
                else:
                    dropped_count += 1
                    unverified_candidates.append(candidate)

            # If some candidates failed verification and we have retry budget, retry once with feedback
            if unverified_candidates and attempt < max_retries:
                attempt += 1
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({
                    "role": "user",
                    "content": (
                        "The snippet you provided did not match the page text verbatim. "
                        "Please re-extract findings ensuring snippets are 100% exact verbatim quotes."
                    ),
                })
            else:
                break

        return verified_findings, dropped_count, attempt + 1

    def execute_subquestion(
        self,
        sub_question: SubQuestion,
        remaining_budget: Optional[int] = None,
        max_pages: int = 3,
    ) -> Tuple[ResearchResult, List[Finding]]:
        failures: List[ToolFailure] = []
        all_findings: List[Finding] = []
        searches_used = 0
        pages_fetched = 0
        llm_calls_used = 0

        # Step 1: Generate Queries
        try:
            queries = self.generate_queries(sub_question, remaining_budget=remaining_budget)
            llm_calls_used += 1
        except Exception as e:
            logger.error("Query generation failed for %s: %s", sub_question.id, e)
            failures.append(ToolFailure(tool="query_gen", status="error", error_message=str(e)))
            return (
                ResearchResult(
                    subquestion_id=sub_question.id,
                    status="error",
                    failures=failures,
                    llm_calls_used=llm_calls_used,
                ),
                [],
            )

        # Step 2: Search
        urls_to_fetch: List[Tuple[str, str, Optional[str]]] = []  # (url, domain, title)
        for q in queries[:2]:  # cap queries dispatched
            searches_used += 1
            search_res = self.search_chain.search(q, max_results=4, max_per_domain=2)
            if search_res.status != "ok":
                failures.append(
                    ToolFailure(
                        tool="search",
                        status=search_res.status,
                        error_message=search_res.error_message or "Search returned no results",
                        target=q,
                    )
                )
            for hit in search_res.results:
                if len(urls_to_fetch) < max_pages:
                    urls_to_fetch.append((hit.url, hit.domain, hit.title))

        maybe_crash("mid_research_after_searches")

        if not urls_to_fetch:
            return (
                ResearchResult(
                    subquestion_id=sub_question.id,
                    status="no_results",
                    searches_used=searches_used,
                    llm_calls_used=llm_calls_used,
                    failures=failures,
                ),
                [],
            )

        # Step 3: Fetch & Extract
        for url, domain, title in urls_to_fetch:
            pages_fetched += 1
            fetch_res = self.fetcher.fetch(url)

            maybe_crash("mid_research_after_fetches")

            if fetch_res.status != "ok":
                failures.append(
                    ToolFailure(
                        tool="fetch",
                        status=fetch_res.status,
                        error_message=fetch_res.error_message or "Fetch failed",
                        target=url,
                    )
                )
                continue

            # Step 4: BM25 passage selection
            paragraphs = split_into_paragraphs(fetch_res.text_content)
            top_passages = select_top_passages_bm25(
                query=sub_question.question,
                paragraphs=paragraphs,
                max_tokens=self.settings.fetch.bm25_max_tokens_per_page,
            )

            if not top_passages:
                continue

            # Step 5: Extract findings & verify provenance
            maybe_crash("mid_extraction")
            try:
                page_findings, dropped, calls_done = self.extract_findings_from_passages(
                    sub_question=sub_question,
                    passages=top_passages,
                    page_text=fetch_res.text_content,
                    source_url=url,
                    domain=domain,
                    page_hash=fetch_res.page_hash,
                    title=title,
                    remaining_budget=remaining_budget,
                )
                llm_calls_used += calls_done
                all_findings.extend(page_findings)
            except Exception as e:
                logger.error("Extraction failed for %s: %s", url, e)
                failures.append(
                    ToolFailure(
                        tool="extract",
                        status="error",
                        error_message=str(e),
                        target=url,
                    )
                )

        # Status determination
        if all_findings:
            status = "ok" if len(all_findings) >= 2 else "partial"
        else:
            status = "no_results" if pages_fetched > 0 else "error"

        result = ResearchResult(
            subquestion_id=sub_question.id,
            status=status,
            finding_ids=[f.finding_id for f in all_findings],
            searches_used=searches_used,
            pages_fetched=pages_fetched,
            llm_calls_used=llm_calls_used,
            failures=failures,
        )

        return result, all_findings
