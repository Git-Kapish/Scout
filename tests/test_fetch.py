import ipaddress

import respx

from scout.fetch.fetcher import PageFetcher
from scout.fetch.ssrf import is_ip_blocked, validate_url_safe


def test_ssrf_guard_blocked_ips():
    assert is_ip_blocked(ipaddress.ip_address("127.0.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("10.0.0.5")) is True
    assert is_ip_blocked(ipaddress.ip_address("172.16.0.1")) is True
    assert is_ip_blocked(ipaddress.ip_address("192.168.1.100")) is True
    assert is_ip_blocked(ipaddress.ip_address("169.254.169.254")) is True
    assert is_ip_blocked(ipaddress.ip_address("8.8.8.8")) is False


def test_ssrf_guard_urls():
    # Direct loopback IP
    safe, reason = validate_url_safe("http://127.0.0.1:8000/secret")
    assert safe is False
    assert "Blocked direct IP destination" in reason

    # Cloud metadata IP
    safe, reason = validate_url_safe("http://169.254.169.254/latest/meta-data/")
    assert safe is False

    # Prohibited scheme
    safe, reason = validate_url_safe("file:///etc/passwd")
    assert safe is False
    assert "Prohibited URL scheme" in reason


@respx.mock
def test_page_fetcher_success(tmp_path):
    url = "https://example.com/research"
    mock_html = (
        "<html><body><article>"
        "<h1>Deep Learning</h1>"
        "<p>Deep learning models process vast amounts of unstructured data efficiently.</p>"
        "<p>They utilize multi-layer neural networks for representation learning.</p>"
        "</article></body></html>"
    )
    respx.get(url).respond(status_code=200, html=mock_html)

    fetcher = PageFetcher(cache_dir=tmp_path)
    result = fetcher.fetch(url)

    assert result.status == "ok"
    assert "Deep learning models process" in result.text_content
    assert result.page_hash != ""

    # Verify disk cache written (PRD FR-4.5)
    cached_file = tmp_path / f"{result.page_hash}.txt"
    assert cached_file.exists()


@respx.mock
def test_page_fetcher_paywall_detection(tmp_path):
    url = "https://example.com/gated"
    respx.get(url).respond(status_code=402, text="Payment Required")

    fetcher = PageFetcher(cache_dir=tmp_path)
    result = fetcher.fetch(url)
    assert result.status == "paywalled"


@respx.mock
def test_page_fetcher_redirect_ssrf_blocked(tmp_path):
    initial_url = "https://example.com/redirect"
    respx.get(initial_url).respond(
        status_code=302,
        headers={"Location": "http://169.254.169.254/credentials"},
    )

    fetcher = PageFetcher(cache_dir=tmp_path)
    result = fetcher.fetch(initial_url)
    assert result.status == "blocked"
    assert "SSRF blocked redirect" in (result.error_message or "")
