"""Planner Agent (PRD FR-1.2).

Decomposes a complex research question into independent, self-contained sub-questions.
Enforces specificity (names entities, metrics, or timeframes) and independence.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import BaseModel, Field

from scout.config import get_settings
from scout.llm import LLMClient
from scout.schemas import Plan, SubQuestion

logger = logging.getLogger(__name__)


class PlannerOutputSchema(BaseModel):
    sub_questions: list[SubQuestion] = Field(
        description="List of 2 to 5 self-contained, independent sub-questions"
    )


PLANNER_SYSTEM_PROMPT = """You are an expert research decomposition specialist.
Your goal is to break down a high-level research question into independent, self-contained sub-questions.

Rules:
1. Each sub-question must be fully self-contained (mentioning specific entities, metrics, technologies, or timeframes).
2. Sub-questions MUST be independent: answering one must not depend on the answer to another.
3. Keep the number of sub-questions between {min_subq} and {max_subq}.
4. Indicate whether recent information is needed (needs_recency: true/false).
5. Output ONLY valid JSON in this exact structure:
{{"sub_questions": [{{"id": "sq_1", "question": "Detailed sub-question...", "rationale": "Why this is needed...", "needs_recency": false}}]}}
"""


class PlannerAgent:
    def __init__(self, llm_client: Optional[LLMClient] = None):
        self.llm_client = llm_client or LLMClient()
        self.settings = get_settings()

    def plan(
        self,
        question: str,
        max_subquestions: Optional[int] = None,
        remaining_budget: Optional[int] = None,
    ) -> Plan:
        max_subq = max_subquestions or self.settings.budgets["default"].max_subquestions
        min_sub = 1 if max_subq <= 1 else 2
        system_content = PLANNER_SYSTEM_PROMPT.format(
            min_subq=min_sub,
            max_subq=max_subq,
        )

        user_content = f"Research Question: {question}\n\nGenerate the research plan."

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]

        resp = self.llm_client.generate(
            role="planner",
            messages=messages,
            response_model=PlannerOutputSchema,
            remaining_token_budget=remaining_budget,
        )

        parsed: PlannerOutputSchema = resp.parsed
        # Clamp to max_subquestions
        sub_questions = parsed.sub_questions[:max_subq]

        # Enforce unique IDs and standard naming
        for idx, sq in enumerate(sub_questions, start=1):
            sq.id = f"sq_{idx}"

        return Plan(schema_version="v1", sub_questions=sub_questions)
