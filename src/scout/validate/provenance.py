"""Snippet Provenance Verification (PRD §4.3 #3, FR-3.3).

Mechanically verifies that candidate finding snippets appear verbatim
in the fetched page text (whitespace-normalized).
"""

from __future__ import annotations

import re


def normalize_whitespace(text: str) -> str:
    """Collapses tabs, newlines, and multiple spaces into a single space, stripping edges."""
    if not text:
        return ""
    # Standardize curly quotes to straight quotes
    normalized = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    return re.sub(r"\s+", " ", normalized).strip()


def verify_snippet_in_page(snippet: str, page_text: str) -> bool:
    """Returns True if the normalized snippet appears verbatim inside the normalized page text."""
    if not snippet or not page_text:
        return False

    norm_snippet = normalize_whitespace(snippet)
    norm_page = normalize_whitespace(page_text)

    # Whitespace-normalized verbatim match only (PRD §4.3 #3): looser matching lets
    # altered figures through, e.g. "1.5 billion" vs "15 billion".
    return norm_snippet in norm_page
