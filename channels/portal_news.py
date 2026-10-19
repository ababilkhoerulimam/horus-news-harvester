"""
Portal News Channel for Indonesian Sports.
Implements multi-backend fallback: RSS feeds -> Jina Reader -> Direct HTTP.
"""

from typing import List, Dict, Any, Optional, Tuple
import urllib.request
import re

from .base import SportsChannel
from backends.jina_reader import JinaReaderBackend
from backends.rss_parser import RssParserBackend, SPORT_RSS_FEEDS
from processors.cleaner import clean_news_text


class PortalNewsChannel(SportsChannel):
    """Channel handling Indonesian sports portals (Antara, Detik, Republika, Bola)."""

    name = "portal_news"
    description = "Portal Berita Olahraga Indonesia (Antara, Detik, Republika)"
    backends = ["rss_parser", "jina_reader", "direct_http"]
    tier = 0

    def __init__(self):
        self.jina = JinaReaderBackend()
        self.rss = RssParserBackend()

    def can_handle(self, url_or_source: str) -> bool:
        known_domains = ("antaranews.com", "detik.com", "kompas.com", "republika.co.id", "bola.net")
        return any(domain in url_or_source.lower() for domain in known_domains)

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        rss_status, rss_msg = self.rss.check()
        jina_status, jina_msg = self.jina.check()

        if rss_status == "ok" or jina_status == "ok":
            self.active_backend = "rss_parser" if rss_status == "ok" else "jina_reader"
            return "ok", f"Available backends: RSS ({rss_status}), Jina ({jina_status})"
        self.active_backend = "direct_http"
        return "warn", "External backends degraded, using direct HTTP fallback"

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
        """Extracts article content using ordered backend fallback."""
        backends_to_try = self.ordered_backends(config)

        for backend in backends_to_try:
            if backend == "jina_reader":
                res = self.jina.read_markdown(url)
                if res.get("status") == "success":
                    clean_content = clean_news_text(res.get("content", ""))
                    return {
                        "url": url,
                        "content": clean_content,
                        "backend": "jina_reader",
                        "status": "success",
                    }

            elif backend == "direct_http":
                try:
                    req = urllib.request.Request(
                        url,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        html = resp.read().decode("utf-8", errors="replace")

                    raw_text = re.sub(r"<[^>]+>", " ", html)
                    clean_content = clean_news_text(raw_text)
                    return {
                        "url": url,
                        "content": clean_content,
                        "backend": "direct_http",
                        "status": "success",
                    }
                except Exception:
                    continue

        return {"url": url, "content": "", "backend": "none", "status": "failed"}
