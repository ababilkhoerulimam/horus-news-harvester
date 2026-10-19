"""
RSS Feed Parser Backend for Indonesian sports news channels.
Provides lightweight XML/Atom stream parsing with zero API dependencies.
"""

import xml.etree.ElementTree as ET
import urllib.request
import re
import time
from typing import List, Dict, Any, Tuple, Optional
from email.utils import parsedate_to_datetime
from datetime import datetime

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

# Known sports RSS feeds for Indonesian media
SPORT_RSS_FEEDS = {
    "antara_olahraga": "https://www.antaranews.com/rss/olahraga.xml",
    "republika_sepakbola": "https://www.republika.co.id/rss/sepakbola",
    "republika_arena": "https://www.republika.co.id/rss/arena",
    "sindonews_olahraga": "https://sports.sindonews.com/rss",
    "okezone_sports": "https://sindikasi.okezone.com/index.php/rss/2/sport",
}


def clean_xml_text(text: str) -> str:
    """Strips CDATA markers, HTML tags, and trailing whitespace."""
    if not text:
        return ""
    # Strip HTML tags
    cleaned = re.sub(r"<[^>]+>", "", text)
    # Normalize whitespaces
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def normalize_pubdate(date_str: str) -> str:
    """Normalizes various RFC822 / ISO date strings into YYYY-MM-DD HH:MM:SS."""
    if not date_str:
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        # Fallback extract year-month-day regex
        match = re.search(r"(\d{4})[-/](\d{2})[-/](\d{2})", date_str)
        if match:
            return f"{match.group(1)}-{match.group(2)}-{match.group(3)} 00:00:00"
        return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def _find_elem(parent: ET.Element, tags: List[str]) -> Optional[ET.Element]:
    for tag in tags:
        el = parent.find(tag)
        if el is not None:
            return el
    return None


class RssParserBackend:
    """Fetches and parses RSS/Atom feeds from sports portals."""

    name = "rss_parser"

    def __init__(self, timeout: int = 15, cache_ttl: int = 300):
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self._feed_cache: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    def check(self) -> Tuple[str, str]:
        """Probes reachability of primary RSS endpoints."""
        try:
            req = urllib.request.Request(
                SPORT_RSS_FEEDS["antara_olahraga"],
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    return "ok", "Indonesian Sports RSS feeds accessible"
            return "warn", f"RSS returned HTTP {resp.status}"
        except Exception as e:
            return "warn", f"RSS probe failed: {e}"

    def parse_feed_url(self, feed_url: str, portal_tag: str = "general") -> List[Dict[str, Any]]:
        """Parses a specific RSS feed URL and returns standardized list of articles."""
        now = time.time()
        if feed_url in self._feed_cache:
            ts, cached_articles = self._feed_cache[feed_url]
            if (now - ts) < self.cache_ttl:
                return cached_articles

        req = urllib.request.Request(
            feed_url,
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/xml, text/xml, */*"},
        )

        articles = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)
            # Standard RSS 2.0 uses channel -> item
            items = root.findall(".//item")
            # Atom feeds use entry
            if not items:
                items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for item in items:
                title_elem = _find_elem(item, ["title", "{http://www.w3.org/2005/Atom}title"])
                link_elem = _find_elem(item, ["link", "{http://www.w3.org/2005/Atom}link"])
                date_elem = _find_elem(
                    item,
                    ["pubDate", "{http://www.w3.org/2005/Atom}published", "{http://www.w3.org/2005/Atom}updated"]
                )
                desc_elem = _find_elem(
                    item,
                    ["description", "{http://www.w3.org/2005/Atom}summary", "{http://www.w3.org/2005/Atom}content"]
                )
                author_elem = _find_elem(
                    item,
                    ["author", "{http://purl.org/dc/elements/1.1/}creator", "{http://www.w3.org/2005/Atom}author"]
                )

                title = clean_xml_text(title_elem.text if title_elem is not None else "")

                url = ""
                if link_elem is not None:
                    url = link_elem.text or link_elem.get("href", "")
                url = url.strip()

                pub_date = normalize_pubdate(date_elem.text if date_elem is not None else "")
                summary = clean_xml_text(desc_elem.text if desc_elem is not None else "")
                author = clean_xml_text(author_elem.text if author_elem is not None else None)

                if title and url:
                    articles.append({
                        "title": title,
                        "url": url,
                        "published_date": pub_date,
                        "summary": summary,
                        "author": author or portal_tag,
                        "source": portal_tag,
                        "backend": self.name,
                    })

            if articles:
                self._feed_cache[feed_url] = (now, articles)

        except Exception:
            pass

        return articles
