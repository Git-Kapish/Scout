"""Crash Trial Execution Subprocess (PRD FR-4.6).

Invoked by test_crash_matrix to simulate hard worker death at exact crash points
and verify resume from committed checkpoints.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from unittest.mock import MagicMock

from scout.agents.planner import PlannerAgent, PlannerOutputSchema
from scout.agents.researcher import (
    CandidateFinding,
    ExtractionOutput,
    QueryGenerationOutput,
    ResearcherAgent,
)
from scout.agents.writer import WriterAgent
from scout.fetch.fetcher import PageFetchResult
from scout.graph.supervisor import SupervisorReviewer
from scout.llm import LLMResponse, LLMUsage
from scout.schemas import Claim, Report, ReportSection, SubQuestion
from scout.search import FixtureSearchAdapter, SearchChain, extract_domain
from scout.worker import ResearchWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")


def make_mock_llm_resp(parsed) -> LLMResponse:
    return LLMResponse(
        content=str(parsed),
        parsed=parsed,
        provider="fake",
        model="fake-model",
        usage=LLMUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100),
        latency_s=0.01,
    )


def build_deterministic_harness():
    subqs = [
        SubQuestion(id="sq_1", question="What is A?", rationale="r1"),
        SubQuestion(id="sq_2", question="What is B?", rationale="r2"),
    ]
    pages = {
        "https://test.org/page1": "Page 1 contains key fact alpha verified verbatim for question.",
        "https://test2.org/page2": "Page 2 contains key fact beta verified verbatim for question.",
    }

    mock_planner_llm = MagicMock()
    mock_planner_llm.estimate_prompt_tokens.return_value = 50
    mock_planner_llm.generate.return_value = make_mock_llm_resp(
        PlannerOutputSchema(sub_questions=subqs)
    )
    planner = PlannerAgent(llm_client=mock_planner_llm)

    search_adapter = FixtureSearchAdapter()
    for q in ["query_1", "query_2", "targeted research query"]:
        hits = [
            {"title": f"Doc for {url}", "url": url, "snippet": "snippet", "domain": extract_domain(url)}
            for url in pages.keys()
        ]
        search_adapter.register_fixture(q, hits)
    search_chain = SearchChain(providers=[search_adapter])

    mock_fetcher = MagicMock()
    mock_fetcher.fetch.side_effect = lambda url: PageFetchResult(
        url=url, status="ok", text_content=pages[url], page_hash=f"h_{abs(hash(url))}", status_code=200
    )

    mock_researcher_llm = MagicMock()
    mock_researcher_llm.estimate_prompt_tokens.return_value = 50

    candidates = [
        CandidateFinding(claim="Fact alpha", snippet="key fact alpha verified verbatim"),
        CandidateFinding(claim="Fact beta", snippet="key fact beta verified verbatim"),
    ]

    def researcher_generate(role, messages, response_model=None, **kwargs):
        if response_model == QueryGenerationOutput:
            return make_mock_llm_resp(QueryGenerationOutput(queries=["targeted research query"]))
        elif response_model == ExtractionOutput:
            return make_mock_llm_resp(ExtractionOutput(findings=candidates))
        return make_mock_llm_resp(None)

    mock_researcher_llm.generate.side_effect = researcher_generate
    researcher = ResearcherAgent(llm_client=mock_researcher_llm, search_chain=search_chain, fetcher=mock_fetcher)

    supervisor = SupervisorReviewer()

    mock_writer_llm = MagicMock()
    mock_writer_llm.estimate_prompt_tokens.return_value = 50
    writer = WriterAgent(llm_client=mock_writer_llm)

    def valid_writer(question, plan, findings, **kwargs):
        fids = [f.finding_id for f in findings]
        return Report(
            title="Crash Trial Report",
            summary="Summary",
            sections=[ReportSection(title="S1", content="C", claims=[Claim(text="Claim A", finding_ids=fids[:2])])],
            coverage_gaps=[],
            markdown="# Crash Trial Report",
        )

    writer.write = MagicMock(side_effect=valid_writer)
    return planner, researcher, supervisor, writer


def main():
    parser = argparse.ArgumentParser(description="Run crash trial subprocess")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--action", choices=["start", "resume"], default="start")
    parser.add_argument("--crash-at", default=None)
    parser.add_argument("--redis-url", default="redis://127.0.0.1:6379/0")
    args = parser.parse_args()

    if args.crash_at:
        os.environ["SCOUT_FAULTS_ENABLED"] = "1"
        os.environ["SCOUT_CRASH_AT"] = args.crash_at
    else:
        os.environ.pop("SCOUT_FAULTS_ENABLED", None)
        os.environ.pop("SCOUT_CRASH_AT", None)

    worker = ResearchWorker(redis_url=args.redis_url, worker_id=f"worker_{os.getpid()}")
    planner, researcher, supervisor, writer = build_deterministic_harness()

    # Wire EffectJournal to exercise before_intent, after_intent, etc.
    from scout.effects.journal import EffectJournal
    from scout.search.base import SearchResult
    journal = EffectJournal(worker.r)
    orig_search = researcher.search_chain.search

    def durable_search(query, **kw):
        res, _ = journal.execute_durable(
            run_id=args.run_id,
            step="search_step",
            effect_type="search",
            input_data={"q": query},
            dispatch_fn=lambda: orig_search(query, **kw),
            serialize_fn=lambda r: r.model_dump(),
            deserialize_fn=lambda d: SearchResult.model_validate(d),
        )
        return res

    researcher.search_chain.search = durable_search

    # Handle cancellation crash point
    run_options = {}
    if args.crash_at == "during_cancellation":
        run_options["cancellation_requested"] = True

    try:
        if args.action == "start":
            state = worker.execute_run(
                run_id=args.run_id,
                initial_question="Crash test question?",
                options=run_options,
                planner_agent=planner,
                researcher_agent=researcher,
                writer_agent=writer,
                supervisor_reviewer=supervisor,
            )
        else:
            state = worker.execute_run(
                run_id=args.run_id,
                force_lease=True,
                options=run_options,
                planner_agent=planner,
                researcher_agent=researcher,
                writer_agent=writer,
                supervisor_reviewer=supervisor,
            )
        print(f"RESULT:status={state.status}")
        sys.exit(0)
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"ERROR:{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
