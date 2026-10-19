"""URL Canonicalization utility.

Strips tracking query parameters, fragments, trailing slashes,
and normalizes schemes and hostnames for reliable deduplication (PRD FR-3.1).
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# Common tracking parameters to strip
TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "ref",
    "source",
}


def canonicalize_url(url: str) -> str:
    """Returns a canonicalized representation of the given URL."""
    if not url:
        return ""

    url = url.strip()
    parsed = urlparse(url)

    # Force lowercase scheme and hostname
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()

    # Default to https if no scheme
    if not scheme:
        scheme = "https"

    # Strip default ports
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]

    # Normalize path: remove redundant slashes, trailing slash for non-root
    path = parsed.path
    path = re.sub(r"/+", "/", path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if not path:
        path = "/"

    # Filter out tracking query parameters
    filtered_query = []
    if parsed.query:
        for k, v in parse_qsl(parsed.query):
            if k.lower() not in TRACKING_PARAMS:
                filtered_query.append((k, v))
        filtered_query.sort()

    query_str = urlencode(filtered_query)

    # Strip fragments completely (PRD FR-3.1)
    fragment = ""

    return urlunparse((scheme, netloc, path, parsed.params, query_str, fragment))


def extract_domain(url: str) -> str:
    """Extracts the registered domain or host from a URL."""
    try:
        netloc = urlparse(url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        # Remove port if present
        if ":" in netloc:
            netloc = netloc.split(":")[0]
        return netloc
    except Exception:
        return ""
