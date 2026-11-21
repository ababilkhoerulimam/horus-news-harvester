"""Shared extraction ladder: direct fetch, proxy retry, channel fallback."""

import asyncio
from typing import NamedTuple, Optional
from urllib.parse import urlparse

from typing import Dict, Optional, Tuple

from backends.wordpress_rest import fetch_post_body
from channels.portal_news import extract_clean_article_text, is_cloudflare_challenge
from processors import session_store
from processors.cleaner import normalize_article
from processors.content_policy import REJECT, shape_decision
from processors.http_client import default_http_client
from processors.record_writer import MIN_QUALITY_CHARS

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
REASON_REJECTED = "Content shape rejected"


class FetchResult(NamedTuple):
    """Outcome of one article extraction attempt.

    quality/content_chars/label are carried on the success path so the caller
    can tag the record without measuring the body a second time.
    """

    content: Optional[str]
    backend: Optional[str]
    reason: Optional[str]
    title: str = ""
    published_date: str = ""
    content_chars: int = 0
    quality: str = "ok"
    label: str = ""


def _success(
    content: str,
    backend: str,
    title: str = "",
    published_date: str = "",
    url: str = "",
) -> FetchResult:
    """Builds a result carrying the body's shape label and quality.

    A rejected shape returns no content. An error page or a reader envelope is
    not an article, and writing one into a record is what put 7.344 envelope
    bodies into the corpus; the caller keeps the reason so the frontier row
    says why the URL produced nothing.
    """
    label, quality, _ = shape_decision(content, url)
    if quality == REJECT:
        return FetchResult(None, backend, REASON_REJECTED, quality=REJECT, label=label)
    return FetchResult(content, backend, None, title, published_date, len(content), quality, label)


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

    `min_quality_chars` is still accepted for callers that pass it and no longer
    has any effect: quality comes from the body's shape, not its length.
    """
    if fallback_min_content_length is None:
        fallback_min_content_length = min_content_length

    # The last body the shape rules rejected. Kept so a URL that only ever
    # produced an error page reports that instead of "too short".
    rejected: Optional[FetchResult] = None

    if wp_api_base:
        post = await fetch_post_body(session, curl_backend, wp_api_base, url)
        if post:
            content = normalize_article(post.get("content") or "")
            if len(content) >= min_content_length:
                result = _success(content, "wordpress_rest", post.get("title") or "", url=url)
                if result.content is not None:
                    return result
                rejected = result

    domain = _domain_of(url)

    def _accept(status: int, html: Optional[str], backend: str) -> Optional[FetchResult]:
        """Returns a result when this response is good enough to keep."""
        nonlocal rejected
        if status != 200 or not html or is_cloudflare_challenge(status, html):
            return None
        # Normalize before the gate: a body that is only a dateline and a
        # cross-link line should not pass just because those lines are long.
        content = normalize_article(extract_clean_article_text(html, domain))
        if len(content) < min_content_length:
            return None
        result = _success(content, backend, url=url)
        if result.content is None:
            # A rejected shape means this rung produced no article, so the next
            # rung still gets a turn: an envelope comes from the fetch, not the URL.
            rejected = result
            return None
        return result

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
        content = normalize_article(res.get("content") or "")
        if len(content) >= fallback_min_content_length:
            result = _success(
                content,
                res.get("backend", "direct_http"),
                res.get("title") or "",
                res.get("published_date") or "",
                url,
            )
            if result.content is not None:
                return result
            rejected = result
    return rejected or FetchResult(None, None, REASON_TOO_SHORT)
