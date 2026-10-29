"""Shared extraction ladder: direct fetch, proxy retry, channel fallback."""

import asyncio
from typing import NamedTuple, Optional
from urllib.parse import urlparse

from typing import Dict, Optional, Tuple

from backends.wordpress_rest import fetch_post_body
from channels.portal_news import extract_clean_article_text, is_cloudflare_challenge
from processors import session_store
from processors.http_client import default_http_client
from processors.record_writer import MIN_QUALITY_CHARS, quality_of

RETRY_STATUSES = (0, 202, 403, 429, 503)


def _domain_of(url: str) -> str:
    """Bare domain used for extraction rule lookup."""
    try:
        return urlparse(url).netloc.replace("www.", "").lower()
    except Exception:
        return ""


# domain -> (cookies, user_agent) or None. Sessions are static for a run, and a
# harvest touches the same handful of domains thousands of times.
_domain_sessions: Dict[str, Optional[Tuple[Dict[str, str], Optional[str]]]] = {}


def domain_session(domain: str) -> Optional[Tuple[Dict[str, str], Optional[str]]]:
    """Returns the stored browser session for a domain, reading the file at most once."""
    if not domain:
        return None
    if domain not in _domain_sessions:
        cookies = session_store.load(domain)
        _domain_sessions[domain] = (
            (cookies, session_store.load_user_agent(domain)) if cookies else None
        )
    return _domain_sessions[domain]


def reset_domain_sessions() -> None:
    """Forgets cached sessions so the next lookup re-reads disk."""
    _domain_sessions.clear()


def _is_blocked(status: int, html: Optional[str]) -> bool:
    """True when the response is not a page the extractor should bother reading."""
    return status in RETRY_STATUSES or not html or is_cloudflare_challenge(status, html)
REASON_NOT_FOUND = "HTTP 404 Not Found"
REASON_TOO_SHORT = "Content length below minimum threshold"


class FetchResult(NamedTuple):
    """Outcome of one article extraction attempt.

    quality/content_chars are carried on the success path so the caller can tag
    the record without measuring the body a second time.
    """

    content: Optional[str]
    backend: Optional[str]
    reason: Optional[str]
    title: str = ""
    published_date: str = ""
    content_chars: int = 0
    quality: str = "ok"


def _success(
    content: str,
    backend: str,
    title: str = "",
    published_date: str = "",
    min_quality_chars: int = MIN_QUALITY_CHARS,
) -> FetchResult:
    """Builds a success result, labelling short bodies instead of dropping them."""
    chars = len(content)
    return FetchResult(
        content,
        backend,
        None,
        title,
        published_date,
        chars,
        quality_of(chars, min_quality_chars),
    )


async def fetch_article(
    session,
    curl_backend,
    channel,
    url: str,
    min_content_length: int,
    fallback_min_content_length: Optional[int] = None,
    wp_api_base: Optional[str] = None,
    min_quality_chars: int = MIN_QUALITY_CHARS,
) -> FetchResult:
    """Fetches and cleans one article, falling back to the channel extractor.

    When the portal exposes WordPress REST, the article body is read from JSON
    first: the response is tens of kilobytes instead of hundreds, and it is the
    publisher's own markup rather than a theme scrape.

    A 404 short-circuits without proxy retry because the page is gone. Any other
    blocked or challenged response is retried once through the rotating proxy
    before the channel extractor is consulted.
    """
    if fallback_min_content_length is None:
        fallback_min_content_length = min_content_length

    if wp_api_base:
        post = await fetch_post_body(session, curl_backend, wp_api_base, url)
        if post and len(post["content"]) >= min_content_length:
            return _success(
                post["content"],
                "wordpress_rest",
                post.get("title") or "",
                min_quality_chars=min_quality_chars,
            )

    domain = _domain_of(url)

    def _accept(status: int, html: Optional[str], backend: str) -> Optional[FetchResult]:
        """Returns a result when this response is good enough to keep."""
        if status != 200 or not html or is_cloudflare_challenge(status, html):
            return None
        content = extract_clean_article_text(html, domain)
        if len(content) < min_content_length:
            return None
        return _success(content, backend, min_quality_chars=min_quality_chars)

    if session is not None:
        status, html = await curl_backend.fetch_async(session, url, proxy=None)
        if status == 404:
            return FetchResult(None, None, REASON_NOT_FOUND)

        accepted = _accept(status, html, "curl_impersonate")
        if accepted is not None:
            return accepted

        if _is_blocked(status, html):
            proxy = default_http_client.get_active_proxy()
            if proxy:
                status, html = await curl_backend.fetch_async(session, url, proxy=proxy)
                accepted = _accept(status, html, "curl_impersonate")
                if accepted is not None:
                    return accepted

            # A 202 or a challenge page is the WAF asking who you are, not what
            # you want. Replay the browser session captured for this domain: the
            # cookie carries the clearance the challenge is looking for, and a
            # rotating proxy carries nothing.
            stored = domain_session(domain)
            if stored:
                cookies, user_agent = stored
                status, html = await curl_backend.fetch_async(
                    session, url, cookies=cookies, user_agent=user_agent
                )
                accepted = _accept(status, html, "curl_session")
                if accepted is not None:
                    return accepted

    res = await asyncio.to_thread(channel.extract, url, {"disable_headless": True})
    if res.get("status") == "success":
        content = res.get("content") or ""
        if len(content) >= fallback_min_content_length:
            return _success(
                content,
                res.get("backend", "direct_http"),
                res.get("title") or "",
                res.get("published_date") or "",
                min_quality_chars,
            )
    return FetchResult(None, None, REASON_TOO_SHORT)
