from scout.search import (
    FixtureSearchAdapter,
    SearchHit,
    canonicalize_url,
    extract_domain,
    filter_and_deduplicate_hits,
)


def test_url_canonicalization():
    url = "https://example.com/research//page/?utm_source=twitter&utm_medium=cpc&ref=123#summary"
    c_url = canonicalize_url(url)
    assert c_url == "https://example.com/research/page"
    assert extract_domain(c_url) == "example.com"


def test_search_hit_deduplication_and_domain_cap():
    hits = [
        SearchHit(title="A1", url="https://example.com/page1?utm_campaign=xyz", snippet="s1", domain="example.com"),
        SearchHit(title="A2", url="https://example.com/page1", snippet="s2", domain="example.com"),  # duplicate
        SearchHit(title="A3", url="https://example.com/page2", snippet="s3", domain="example.com"),
        SearchHit(title="A4", url="https://example.com/page3", snippet="s4", domain="example.com"),  # exceeds cap 2
        SearchHit(title="B1", url="https://other.org/page1", snippet="s5", domain="other.org"),
    ]
    filtered = filter_and_deduplicate_hits(hits, max_results=5, max_per_domain=2)
    assert len(filtered) == 3
    domains = [h.domain for h in filtered]
    assert domains.count("example.com") == 2
    assert domains.count("other.org") == 1


def test_fixture_search_adapter():
    adapter = FixtureSearchAdapter()
    adapter.register_fixture(
        "machine learning",
        [
            {"title": "ML Intro", "url": "https://ml.org/intro", "snippet": "An intro", "domain": "ml.org"},
        ],
    )
    result = adapter.search("machine learning")
    assert result.status == "ok"
    assert len(result.results) == 1
    assert result.results[0].title == "ML Intro"
