"""
Portal News Channel for Indonesian Sports.
Implements multi-backend fallback: RSS feeds -> Jina Reader -> Direct HTTP.
"""

from typing import List, Dict, Any, Optional, Tuple, Set
import urllib.request
import urllib.parse
from urllib.parse import urlparse, urljoin
import json
import html as html_lib
import re

from .base import SportsChannel
from backends.curl_impersonate import CurlImpersonateBackend
from backends.jina_reader import JinaReaderBackend
from backends.rss_parser import RssParserBackend, SPORT_RSS_FEEDS
from backends.headless_browser import HeadlessBrowserBackend, is_spa_skeleton
from processors.cleaner import clean_news_text
from processors.http_client import default_http_client

try:
    import trafilatura
    HAS_TRAFILATURA = True
except ImportError:
    trafilatura = None
    HAS_TRAFILATURA = False


def normalize_portal_url(url: str) -> str:
    """
    Transforms multi-page Indonesian news article URLs into full single-page URLs.
    Prevents content truncation on Detik, Kompas, Tribun, Sindonews, BolaSport, JawaPos, etc.
    """
    clean_url = url.strip()
    if not clean_url:
        return ""

    # Detik: ?single=1 renders full text on one page
    if "detik.com" in clean_url:
        if "single=1" not in clean_url:
            clean_url = re.sub(r"/\d{1,2}(\?|$)", r"\1", clean_url)
            clean_url = re.sub(r"([?&])page=\d+", "", clean_url).rstrip("&?")
            sep = "&" if "?" in clean_url else "?"
            return f"{clean_url}{sep}single=1"
        return clean_url

    # Sindonews: supports ?page=all or ?showpage=all
    if "sindonews.com" in clean_url:
        if "page=all" not in clean_url and "showpage=all" not in clean_url:
            clean_url = re.sub(r"/\d{1,2}(\?|$)", r"\1", clean_url)
            clean_url = re.sub(r"([?&])(?:page|showpage)=\d+", "", clean_url).rstrip("&?")
            sep = "&" if "?" in clean_url else "?"
            return f"{clean_url}{sep}page=all"
        return clean_url

    # Portals supporting ?page=all parameter
    single_page_portals = (
        "kompas.com",
        "tribunnews.com",
        "bolasport.com",
        "bola.com",
        "bola.net",
        "inews.id",
        "liputan6.com",
        "suara.com",
        "merdeka.com",
        "tempo.co",
        "jawapos.com",
        "okezone.com",
        "viva.co.id",
        "grid.id",
        "jpnn.com",
    )
    if any(domain in clean_url for domain in single_page_portals):
        if "page=all" not in clean_url and not clean_url.endswith("/all"):
            clean_url = re.sub(r"/\d{1,2}(\?|$)", r"\1", clean_url)
            clean_url = re.sub(r"([?&])page=\d+", "", clean_url).rstrip("&?")
            sep = "&" if "?" in clean_url else "?"
            return f"{clean_url}{sep}page=all"
        return clean_url

    return clean_url


def find_next_page_url(html: str, current_url: str) -> Optional[str]:
    """
    Discovers the URL of the next paginated page from an Indonesian news portal HTML.
    Supports canonical rel="next", textual pagination ("Halaman Selanjutnya"),
    and numbered pagination buttons.
    """
    if not html:
        return None

    # 1. Canonical rel="next" links (<link rel="next" href="..."> or <a rel="next" href="...">)
    rel_next_patterns = (
        r'<link\s+[^>]*rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']',
        r'<link\s+[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
        r'<a\s+[^>]*rel=["\']next["\'][^>]*href=["\']([^"\']+)["\']',
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*rel=["\']next["\']',
    )
    for pat in rel_next_patterns:
        match = re.search(pat, html, re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if candidate and candidate != "#" and not candidate.startswith("javascript:"):
                return urllib.parse.urljoin(current_url, candidate)

    # 2. Textual pagination links: "Halaman Selanjutnya", "Selanjutnya", "Next Page"
    text_next_patterns = (
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(?:(?!</a>).)*?(?:halaman\s+selanjutnya|halaman\s+berikutnya|selanjutnya|next\s+page)(?:(?!</a>).)*?</a>',
        r'<a\s+[^>]*>(?:(?!</a>).)*?(?:halaman\s+selanjutnya|halaman\s+berikutnya|selanjutnya|next\s+page)(?:(?!</a>).)*?href=["\']([^"\']+)["\']',
    )
    for pat in text_next_patterns:
        match = re.search(pat, html, re.IGNORECASE | re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            if candidate and candidate != "#" and not candidate.startswith("javascript:"):
                return urllib.parse.urljoin(current_url, candidate)

    # 3. Numbered pagination widget discovery (e.g. class="paging" / "pagination" / "detail__body-pagination")
    # Finds the link corresponding to current_page + 1
    page_match = re.search(r'[?&]page=(\d+)', current_url, re.IGNORECASE) or re.search(r'/(\d{1,2})(?:[?#]|$)', current_url)
    current_page = int(page_match.group(1)) if page_match else 1
    next_page_num = current_page + 1

    paging_block_match = re.search(
        r'<(?:div|ul|nav)[^>]*class=["\'][^"\']*(?:paging|pagination|page-nav|detail__body-pagination)[^"\']*["\'][^>]*>(.*?)</(?:div|ul|nav)>',
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if paging_block_match:
        block_html = paging_block_match.group(1)
        target_pat = rf'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>\s*{next_page_num}\s*</a>'
        num_match = re.search(target_pat, block_html, re.IGNORECASE)
        if not num_match:
            param_pat = rf'<a\s+[^>]*href=["\']([^"\']*(?:[?&]page=|\/){next_page_num}(?:[?&#"\']|$)[^"\']*)["\']'
            num_match = re.search(param_pat, block_html, re.IGNORECASE)
        if num_match:
            candidate = num_match.group(1).strip()
            if candidate and candidate != "#" and not candidate.startswith("javascript:"):
                return urllib.parse.urljoin(current_url, candidate)

    return None


def extract_clean_article_text(html: str) -> str:
    """
    Extracts article text from HTML, prioritizing trafilatura for clean body
    extraction, falling back to regex tag stripping and known container matching.
    """
    if not html:
        return ""

    # 0. Fast-path: Inertia.js JSON payload (used by Jawa Pos Group: KaltengPos, RiauPos, Radar, etc.)
    if 'data-page="' in html:
        try:
            match = re.search(r'data-page="([^"]+)"', html)
            if match:
                page_data = json.loads(html_lib.unescape(match.group(1)))
                props = page_data.get("props", {})
                article_data = (
                    props.get("article")
                    or props.get("post")
                    or props.get("detail")
                    or props.get("data")
                    or props.get("news")
                    or {}
                )
                if isinstance(article_data, dict) and article_data.get("content"):
                    raw_content = article_data.get("content", "")
                    clean_txt = clean_news_text(re.sub(r"<[^>]+>", " ", raw_content))
                    if len(clean_txt) >= 100:
                        return clean_txt
        except Exception:
            pass

    # 1. Primary: Trafilatura (cleans CSS, inline JS, ad widgets, nav links for DAPT NLP)
    if HAS_TRAFILATURA and trafilatura is not None:
        try:
            # ponytail: standard trafilatura extraction, add custom lxml xpath if specific portal structure fails
            extracted = trafilatura.extract(
                html,
                include_links=False,
                include_images=False,
                include_tables=False,
                no_fallback=False,
            )
            if extracted:
                cleaned = clean_news_text(extracted)
                if len(cleaned) >= 100:
                    return cleaned
        except Exception:
            pass

    # 2. Fallback: Strip non-content blocks: script, style, noscript, header, footer, nav, aside
    cleaned = re.sub(
        r"<(?:script|style|noscript|header|footer|nav|aside)[^>]*>.*?</(?:script|style|noscript|header|footer|nav|aside)>",
        " ",
        html,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(r"<!--.*?-->", " ", cleaned, flags=re.DOTALL)

    # 3. Try known Indonesian news article containers to exclude sidebars and comment widgets
    article_patterns = (
        r'<div[^>]+class=["\'][^"\']*(?:detail__body-text|itp_bodycontent|read__content|txt-article|article-content-body|n-detail-content|detail-konten|entry-content|post-content)[^"\']*["\'][^>]*>(.*?)</div>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]+id=["\'](?:detikdetailtext|content|article-body)["\'][^>]*>(.*?)</div>',
    )
    for pat in article_patterns:
        match = re.search(pat, cleaned, flags=re.DOTALL | re.IGNORECASE)
        if match:
            candidate_html = match.group(1)
            raw_text = re.sub(r"<[^>]+>", " ", candidate_html)
            cleaned_text = clean_news_text(raw_text)
            if len(cleaned_text) >= 150:
                return cleaned_text

    # 4. Fallback: extract from entire page
    raw_text = re.sub(r"<[^>]+>", " ", cleaned)
    return clean_news_text(raw_text)


def consolidate_article_pages(content_parts: List[str]) -> str:
    """
    Merges text from multiple paginated pages, stripping duplicate paragraphs
    or repeated introductory blurbs across page boundaries.
    """
    if not content_parts:
        return ""
    if len(content_parts) == 1:
        return content_parts[0]

    seen_paragraphs = set()
    combined_paragraphs = []

    for part in content_parts:
        paragraphs = [p.strip() for p in re.split(r"\n+", part) if p.strip()]
        for p in paragraphs:
            p_norm = re.sub(r"[^\w\s]", "", p.lower())
            p_norm = re.sub(r"\s+", " ", p_norm).strip()
            if not p_norm or len(p_norm) < 15:
                if p not in combined_paragraphs:
                    combined_paragraphs.append(p)
                continue

            if p_norm not in seen_paragraphs:
                seen_paragraphs.add(p_norm)
                combined_paragraphs.append(p)

    return "\n\n".join(combined_paragraphs)


def is_cloudflare_challenge(status: int, html: str) -> bool:
    """
    Detects Cloudflare Turnstile, anti-bot challenge, or HTTP 403 challenge pages.
    """
    if not html and status not in (403, 503):
        return False

    if status in (403, 503) and not html:
        return True

    lower = html.lower()
    cf_markers = (
        "just a moment...",
        "cf-chl-bypass",
        "challenge-running",
        "cf-turnstile",
        "attention required! | cloudflare",
        "cloudflare ray id",
        "id=\"cf-challenge-running\"",
        "class=\"cf-browser-verification\"",
        "checking your browser before accessing",
    )
    if any(marker in lower for marker in cf_markers):
        return True

    # If status is 403 or 503 and page has minimal content with cloudflare mention
    if status in (403, 503) and ("cloudflare" in lower or len(html) < 2000):
        return True

    return False


class PortalNewsChannel(SportsChannel):
    """Channel handling Indonesian sports portals (Antara, Detik, Republika, Bola)."""

    name = "portal_news"
    description = "Portal Berita Olahraga Indonesia (Antara, Detik, Republika)"
    backends = ["rss_parser", "curl_impersonate", "jina_reader", "direct_http", "headless_browser"]
    tier = 0

    def __init__(self):
        self.jina = JinaReaderBackend()
        self.rss = RssParserBackend()
        self.headless = HeadlessBrowserBackend()
        self.curl = CurlImpersonateBackend()

    def can_handle(self, url_or_source: str) -> bool:
        known_domains = (
            "antaranews.com",
            "detik.com",
            "kompas.com",
            "tribunnews.com",
            "republika.co.id",
            "bolasport.com",
            "bola.com",
            "bola.net",
            "sindonews.com",
            "jawapos.com",
            "inews.id",
            "okezone.com",
            "liputan6.com",
            "suara.com",
            "merdeka.com",
            "tempo.co",
            "viva.co.id",
            "jpnn.com",
            "grid.id",
        )
        return any(domain in url_or_source.lower() for domain in known_domains)

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        rss_status, rss_msg = self.rss.check()
        curl_status, curl_msg = self.curl.check()
        jina_status, jina_msg = self.jina.check()
        headless_status, headless_msg = self.headless.check()

        if rss_status == "ok" or curl_status == "ok" or jina_status == "ok":
            if rss_status == "ok":
                self.active_backend = "rss_parser"
            elif curl_status == "ok":
                self.active_backend = "curl_impersonate"
            else:
                self.active_backend = "jina_reader"
            return "ok", f"Available backends: RSS ({rss_status}), Curl ({curl_status}), Jina ({jina_status}), Headless ({headless_status})"
        self.active_backend = "direct_http"
        return "warn", "External backends degraded, using direct HTTP / headless fallback"

    def search(
        self,
        query: str,
        sport: str = "football",
        limit: int = 10,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Searches available RSS streams and filters by query keyword."""
        results = []
        q_lower = query.lower()

        for feed_tag, feed_url in SPORT_RSS_FEEDS.items():
            articles = self.rss.parse_feed_url(feed_url, portal_tag=feed_tag)
            for art in articles:
                if q_lower in art["title"].lower() or q_lower in art["summary"].lower():
                    art["sport"] = sport
                    results.append(art)
                    if len(results) >= limit:
                        return results

        return results

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extracts article content using ordered backend fallback with multi-page handling."""
        normalized_url = normalize_portal_url(url)
        backends_to_try = self.ordered_backends(config)

        for backend in backends_to_try:
            if backend == "curl_impersonate" and self.curl.is_available():
                try:
                    proxy = config.get("proxy") if config else None
                    status, html = self.curl.fetch(normalized_url, proxy=proxy)
                    if status == 200 and html:
                        if not is_cloudflare_challenge(status, html):
                            clean_content = extract_clean_article_text(html)
                            if clean_content and len(clean_content) >= 100:
                                return {
                                    "url": normalized_url,
                                    "content": clean_content,
                                    "backend": "curl_impersonate",
                                    "status": "success",
                                }
                except Exception:
                    pass

            elif backend == "jina_reader":
                res = self.jina.read_markdown(normalized_url)
                if res.get("status") == "success":
                    clean_content = clean_news_text(res.get("content", ""))
                    return {
                        "url": normalized_url,
                        "content": clean_content,
                        "backend": "jina_reader",
                        "status": "success",
                    }

            elif backend == "direct_http":
                try:
                    content_parts = []
                    current_url = normalized_url
                    visited = set()
                    raw_html_first_page = ""

                    # ponytail: Max 5 paginated pages to consolidate deep multi-page articles without infinite loops; upgrade to headless if infinite scroll needed.
                    for page_idx in range(5):
                        if not current_url or current_url in visited:
                            break
                        visited.add(current_url)

                        status, body, _ = default_http_client.fetch(current_url, timeout=6.0, max_retries=1)
                        html = body.decode("utf-8", errors="replace") if body else ""

                        # Cloudflare / WAF challenge detection: escalate immediately to Headless Chromium if enabled
                        if is_cloudflare_challenge(status, html):
                            if "headless_browser" in backends_to_try and not (config and config.get("disable_headless")):
                                res_headless = self.headless.extract(normalized_url)
                                if res_headless.get("status") == "success":
                                    return res_headless
                            break

                        if status != 200 or not body:
                            break
                        if page_idx == 0:
                            raw_html_first_page = html

                        clean_content = extract_clean_article_text(html)
                        if clean_content:
                            content_parts.append(clean_content)

                        # Discover next page link if article continues
                        next_url = find_next_page_url(html, current_url)
                        if next_url and next_url not in visited:
                            current_url = next_url
                        else:
                            break

                    if content_parts:
                        full_content = consolidate_article_pages(content_parts)
                        # Detect if page is an unrendered JS / SPA shell
                        if raw_html_first_page and is_spa_skeleton(raw_html_first_page, full_content):
                            if "headless_browser" in backends_to_try and not (config and config.get("disable_headless")):
                                res_headless = self.headless.extract(normalized_url)
                                if res_headless.get("status") == "success":
                                    return res_headless
                        return {
                            "url": normalized_url,
                            "content": full_content,
                            "backend": "direct_http",
                            "status": "success",
                            "pages_harvested": len(content_parts),
                        }
                except Exception:
                    continue

            elif backend == "headless_browser":
                res = self.headless.extract(normalized_url)
                if res.get("status") == "success":
                    return res

        return {"url": normalized_url, "content": "", "backend": "none", "status": "failed"}
