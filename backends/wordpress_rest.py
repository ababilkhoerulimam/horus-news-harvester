"""
WordPress REST article body reader.

WordPress portals already expose the full article HTML at
/wp-json/wp/v2/posts?slug=<slug>, in a response measured in tens of kilobytes
instead of the hundreds of kilobytes of the rendered page. Fetching that instead of
scraping the page removes both the payload cost and the parsing failures that come
from guessing at a theme's container markup.

Only used when the portal fingerprint says the site exposes WP-REST; see
processors/portal_catalog.wp_api_base_for.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from typing import Any, Dict, Optional
from urllib.parse import quote, urlparse

from processors.cleaner import clean_news_text

_TAG_PATTERN = re.compile(r"<[^>]+>")
_FIELDS = "content.rendered,title.rendered,link"


def slug_from_url(url: str) -> Optional[str]:
    """Extracts the trailing path segment, which is the WordPress post slug."""
    path = urlparse(url).path.rstrip("/")
    if not path:
        return None
    slug = path.rsplit("/", 1)[-1]
    if slug.endswith(".html"):
        slug = slug[: -len(".html")]
    if not slug or "/" in slug:
        return None
    return slug


def endpoint_for(base_api: str, url: str) -> Optional[str]:
    """Builds the single-post REST URL for an article, or None when there is no slug."""
    slug = slug_from_url(url)
    if not slug or not base_api:
        return None
    base = base_api.rstrip("/")
    if not base.endswith("/posts"):
        base = f"{base}/posts"
    return f"{base}?slug={quote(slug)}&_fields={_FIELDS}"


def _strip_tags(raw: str) -> str:
    return html_lib.unescape(_TAG_PATTERN.sub(" ", raw or ""))


def parse_post_payload(body: bytes) -> Optional[Dict[str, str]]:
    """Turns a WP-REST single-post response into {content, title}, or None."""
    if not body:
        return None
    try:
        items = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(items, list) or not items:
        return None
    item: Any = items[0]
    if not isinstance(item, dict):
        return None

    rendered = (item.get("content") or {}).get("rendered") or ""
    if not rendered:
        return None
    content = clean_news_text(_strip_tags(rendered))
    if not content:
        return None

    title = clean_news_text(_strip_tags((item.get("title") or {}).get("rendered") or ""))
    return {"content": content, "title": title}


async def fetch_post_body(
    session: Any,
    curl_backend: Any,
    base_api: Optional[str],
    url: str,
) -> Optional[Dict[str, str]]:
    """Async variant used by the harvest pipeline, sharing the worker's session."""
    endpoint = endpoint_for(base_api or "", url)
    if endpoint is None or session is None:
        return None
    try:
        status, body = await curl_backend.fetch_async(session, endpoint)
    except Exception:
        return None
    if status != 200:
        return None
    return parse_post_payload(body)


def post_body_by_url(
    base_api: Optional[str],
    url: str,
    backend: Any = None,
    timeout: int = 8,
) -> Optional[Dict[str, str]]:
    """Sync variant for tooling and tests."""
    endpoint = endpoint_for(base_api or "", url)
    if endpoint is None:
        return None
    if backend is None:
        from backends.curl_impersonate import CurlImpersonateBackend

        backend = CurlImpersonateBackend(timeout=timeout)
    try:
        status, body = backend.fetch(endpoint)
    except Exception:
        return None
    if status != 200:
        return None
    return parse_post_payload(body.encode("utf-8") if isinstance(body, str) else body)
