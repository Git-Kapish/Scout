from scout.fetch.passage import select_top_passages_bm25, split_into_paragraphs


def test_split_into_paragraphs():
    raw = (
        "Short\n\n"
        "This is paragraph one with enough characters to be considered a real paragraph in the text.\n\n"
        "This is paragraph two, also containing substantive sentences discussing the architecture."
    )
    paras = split_into_paragraphs(raw)
    assert len(paras) == 2
    assert "paragraph one" in paras[0]
    assert "paragraph two" in paras[1]


def test_select_top_passages_bm25():
    paras = [
        "Redis AOF everysec synchronizes writes every second via background threads to prevent latency spikes.",
        "PostgreSQL uses Write-Ahead Logging (WAL) with strict checkpointing intervals.",
        "BM25Okapi balances term frequency saturation and document length normalization via parameters k1 and b.",
    ]
    query = "How does Redis AOF background fsync work?"
    selected = select_top_passages_bm25(query, paras, max_tokens=100, top_k=2)

    assert len(selected) >= 1
    assert "Redis AOF everysec" in selected[0]
