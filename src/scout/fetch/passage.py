"""Passage Selection using BM25 ranking (PRD §5.3 #3, FR-3.3).

Ranks page paragraphs by relevance against the active sub-question,
capping the selected text to ~1,500 tokens before dispatching to the LLM.
"""

from __future__ import annotations

import re
from typing import List

from rank_bm25 import BM25Okapi


def tokenize(text: str) -> List[str]:
    """Simple alphanumeric tokenizer for BM25."""
    return re.findall(r"\w+", text.lower())


def split_into_paragraphs(text: str) -> List[str]:
    """Splits text by double newlines into clean, non-empty paragraphs."""
    raw_paras = re.split(r"\n\s*\n", text)
    cleaned = []
    for p in raw_paras:
        p_clean = p.strip()
        # Drop trivial lines (headers with 2 words, navigation fragments)
        if len(p_clean) >= 40:
            cleaned.append(p_clean)
    return cleaned


def select_top_passages_bm25(
    query: str,
    paragraphs: List[str],
    max_tokens: int = 1500,
    top_k: int = 5,
) -> List[str]:
    """Ranks paragraphs with BM25Okapi and accumulates top passages under max_tokens."""
    if not paragraphs:
        return []

    tokenized_query = tokenize(query)
    if not tokenized_query:
        # Fallback to first few paragraphs
        return paragraphs[:top_k]

    tokenized_corpus = [tokenize(p) for p in paragraphs]
    bm25 = BM25Okapi(tokenized_corpus)

    scores = bm25.get_scores(tokenized_query)
    # Pair paragraphs with index and score, sort descending
    scored = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)

    selected: List[str] = []
    estimated_tokens = 0

    for idx, score in scored[:top_k]:
        para = paragraphs[idx]
        # ~1 token per 3.5 chars
        para_tokens = int(len(para) / 3.5)
        if selected and (estimated_tokens + para_tokens > max_tokens):
            break
        selected.append(para)
        estimated_tokens += para_tokens

    return selected
