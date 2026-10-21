from scout.validate.provenance import normalize_whitespace, verify_snippet_in_page


def test_normalize_whitespace():
    raw = "  This   has \n\n multiple \t lines   and “quotes”.  "
    normalized = normalize_whitespace(raw)
    assert normalized == 'This has multiple lines and "quotes".'


def test_verify_snippet_exact_match():
    page_text = (
        "Under the EU AI Act adopted in 2024, general-purpose models with systemic risk "
        "must adhere to strict adversarial red-teaming requirements."
    )
    snippet = "general-purpose models with systemic risk must adhere to strict adversarial red-teaming"
    assert verify_snippet_in_page(snippet, page_text) is True


def test_verify_snippet_whitespace_variations():
    page_text = "The EU AI Act requires\n\n  adversarial   red-teaming for GPAI models."
    snippet = "The EU AI Act requires adversarial red-teaming"
    assert verify_snippet_in_page(snippet, page_text) is True


def test_verify_snippet_rejection_on_fabrication():
    page_text = "OpenRouter offers access to various open-weight models."
    fabricated_snippet = "OpenRouter offers unlimited free requests for all enterprise accounts."
    assert verify_snippet_in_page(fabricated_snippet, page_text) is False


def test_verify_snippet_rejects_altered_figures_and_case():
    page_text = "Revenue grew 1.5 billion in 2025."
    assert verify_snippet_in_page("Revenue grew 15 billion in 2025.", page_text) is False
    assert verify_snippet_in_page("revenue grew 1.5 billion", page_text) is False
