"""
Historical Sitemap & Date Archive Crawler for Indonesian Sports Portals.
Enables large-scale backfilling (10k - 100k articles) for NLP Domain-Adaptive
Pre-Training (DAPT) and Named Entity Recognition (NER).
"""

import json
import logging
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlparse

# Ensure repository root is on Python module search path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from channels.portal_news import PortalNewsChannel, normalize_portal_url
from processors.cleaner import clean_news_text, normalize_timestamp
from processors.crawl_frontier import CrawlFrontier, DEFAULT_FRONTIER_DB
from processors.http_client import default_http_client
from scripts.local_harvester import _load_existing_ids

logger = logging.getLogger("sitemap_backfill")

# Default football-specific path and title keyword filters
DEFAULT_FOOTBALL_KEYWORDS = [
    "sepakbola",
    "sepak-bola",
    "bola",
    "liga-1",
    "timnas",
    "liga-indonesia",
    "persib",
    "persija",
    "arema",
    "persebaya",
    "pssi",
    "shopee-liga-1",
    "bri-liga-1",
    "piala-presiden",
    "piala-asia",
    "world-cup",
    "kualifikasi-piala-dunia",
]

# Known XML Sitemaps of Indonesian news portals
KNOWN_PORTAL_SITEMAPS: Dict[str, List[str]] = {
    "detik": [
        "https://sport.detik.com/sitemap_sepakbola.xml",
        "https://www.detik.com/sitemap_news.xml",
        "https://www.detik.com/sitemap.xml",
    ],
    "antara": [
        "https://www.antaranews.com/sitemap_news.xml",
        "https://www.antaranews.com/sitemap.xml",
    ],
    "kompas": [
        "https://bola.kompas.com/sitemap.xml",
        "https://www.kompas.com/sitemap.xml",
    ],
    "bolanet": [
        "https://www.bola.net/sitemap.xml",
        "https://www.bola.net/sitemap_news.xml",
    ],
    "sindonews": [
        "https://sports.sindonews.com/sitemap.xml",
        "https://www.sindonews.com/sitemap_news.xml",
    ],
    "skor": [
        "https://skor.id/sitemap.xml",
    ],
}


def parse_sitemap_xml(
    xml_content: str,
    filter_keywords: Optional[List[str]] = None
) -> Tuple_Results:
    """
    Parses XML sitemap string into list of article entries and child sitemaps.
    Handles standard urlset, sitemapindex, and Google News extensions.
    """
    keywords = [k.lower() for k in (filter_keywords or DEFAULT_FOOTBALL_KEYWORDS)]
    articles: List[Dict[str, str]] = []
    child_sitemaps: List[str] = []

    if not xml_content or not xml_content.strip():
        return articles, child_sitemaps

    try:
        root = ET.fromstring(xml_content.strip())
    except ET.ParseError:
        # ponytail: XML parser fallback using regex for malformed sitemap documents
        return _regex_fallback_sitemap(xml_content, keywords)

    # Strip XML namespace if present
    tag = root.tag
    ns = ""
    if "}" in tag:
        ns = tag.split("}")[0] + "}"

    # Case 1: Sitemap Index pointing to sub-sitemaps
    if root.tag.endswith("sitemapindex"):
        for sm in root.findall(f"{ns}sitemap"):
            loc_el = sm.find(f"{ns}loc")
            if loc_el is not None and loc_el.text:
                child_sitemaps.append(loc_el.text.strip())
        return articles, child_sitemaps

    # Case 2: Urlset containing article URLs
    for url_el in root.findall(f"{ns}url"):
        loc_el = url_el.find(f"{ns}loc")
        if loc_el is None or not loc_el.text:
            continue

        raw_url = loc_el.text.strip()
        lastmod_el = url_el.find(f"{ns}lastmod")
        lastmod = lastmod_el.text.strip() if (lastmod_el is not None and lastmod_el.text) else ""

        # Title from news sitemap extension if available
        title = ""
        news_el = url_el.find(f".//{ns}news") or url_el.find(".//news")
        if news_el is not None:
            title_el = news_el.find(f".//{ns}title") or news_el.find(".//title")
            if title_el is not None and title_el.text:
                title = title_el.text.strip()
            pub_date_el = news_el.find(f".//{ns}publication_date") or news_el.find(".//publication_date")
            if pub_date_el is not None and pub_date_el.text and not lastmod:
                lastmod = pub_date_el.text.strip()

        # Check football relevance
        url_lower = raw_url.lower()
        title_lower = title.lower()

        is_match = False
        if not keywords:
            is_match = True
        else:
            for kw in keywords:
                if kw in url_lower or (title_lower and kw in title_lower):
                    is_match = True
                    break

        if is_match:
            articles.append({
                "url": normalize_portal_url(raw_url),
                "published_date": normalize_timestamp(lastmod),
                "title": title,
            })

    return articles, child_sitemaps


# Type alias for clarity
Tuple_Results = tuple[List[Dict[str, str]], List[str]]


def _regex_fallback_sitemap(xml_content: str, keywords: List[str]) -> Tuple_Results:
    """Regex extraction for corrupted or non-standard XML sitemaps."""
    articles = []
    child_sitemaps = []

    # Check for sitemap locs
    sitemap_locs = re.findall(r"<sitemap>\s*<loc>(https?://[^<]+)</loc>", xml_content, re.IGNORECASE)
    if sitemap_locs:
        child_sitemaps.extend(sitemap_locs)
        return articles, child_sitemaps

    # Extract <url> blocks
    url_blocks = re.findall(r"<url>(.*?)</url>", xml_content, re.DOTALL | re.IGNORECASE)
    for block in url_blocks:
        loc_m = re.search(r"<loc>(https?://[^<]+)</loc>", block, re.IGNORECASE)
        if not loc_m:
            continue
        raw_url = loc_m.group(1).strip()
        lastmod_m = re.search(r"<(?:lastmod|publication_date)>([^<]+)</(?:lastmod|publication_date)>", block, re.IGNORECASE)
        lastmod = lastmod_m.group(1).strip() if lastmod_m else ""
        title_m = re.search(r"<title>([^<]+)</title>", block, re.IGNORECASE)
        title = title_m.group(1).strip() if title_m else ""

        url_lower = raw_url.lower()
        if any(kw in url_lower for kw in keywords):
            articles.append({
                "url": normalize_portal_url(raw_url),
                "published_date": normalize_timestamp(lastmod),
                "title": title,
            })

    return articles, child_sitemaps


def fetch_sitemap_urls(
    sitemap_url: str,
    filter_keywords: Optional[List[str]] = None,
    max_urls: int = 500,
    max_depth: int = 2
) -> List[Dict[str, str]]:
    """
    Recursively fetches XML sitemaps, expanding child sitemaps matching football.
    Returns list of discovered article records.
    """
    visited_sitemaps: Set[str] = set()
    to_visit: List[tuple[str, int]] = [(sitemap_url, 0)]
    collected_articles: List[Dict[str, str]] = []
    seen_urls: Set[str] = set()

    keywords = filter_keywords or DEFAULT_FOOTBALL_KEYWORDS

    while to_visit and len(collected_articles) < max_urls:
        current_url, depth = to_visit.pop(0)
        if current_url in visited_sitemaps:
            continue
        visited_sitemaps.add(current_url)

        try:
            status, body_bytes, _ = default_http_client.fetch(current_url, timeout=15)
            if status != 200 or not body_bytes:
                continue
            xml_text = body_bytes.decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Failed to fetch sitemap {current_url}: {e}")
            continue

        articles, child_sitemaps = parse_sitemap_xml(xml_text, filter_keywords=keywords)

        for art in articles:
            u = art["url"]
            if u not in seen_urls:
                seen_urls.add(u)
                collected_articles.append(art)
                if len(collected_articles) >= max_urls:
                    break

        # Queue child sitemaps if depth allows
        if depth < max_depth:
            for child in child_sitemaps:
                child_lower = child.lower()
                # Prioritize sports or news child sitemaps
                if any(k in child_lower for k in ["sport", "bola", "news", "sepakbola", "artikel"]):
                    if child not in visited_sitemaps:
                        to_visit.append((child, depth + 1))
                elif not any(k in child_lower for k in ["video", "foto", "lifestyle", "otomotif", "finance"]):
                    # General index sitemap
                    if child not in visited_sitemaps:
                        to_visit.append((child, depth + 1))

    return collected_articles


def generate_portal_archive_urls(
    portal: str,
    start_date: str,
    end_date: str
) -> List[str]:
    """
    Generates historical date archive URLs for Indonesian sports portals.
    Format of dates: YYYY-MM-DD.
    """
    urls: List[str] = []
    try:
        dt_start = datetime.strptime(start_date, "%Y-%m-%d")
        dt_end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return urls

    if dt_start > dt_end:
        dt_start, dt_end = dt_end, dt_start

    curr = dt_start
    p_lower = portal.lower().strip()

    while curr <= dt_end:
        y = curr.strftime("%Y")
        m = curr.strftime("%m")
        d = curr.strftime("%d")
        iso = curr.strftime("%Y-%m-%d")

        if "detik" in p_lower:
            # Detik Sport Sepakbola index: https://sport.detik.com/sepakbola/indeks?date=MM/DD/YYYY
            urls.append(f"https://sport.detik.com/sepakbola/indeks?date={m}%2F{d}%2F{y}")
        elif "antara" in p_lower:
            # Antara Sepak Bola archive: https://www.antaranews.com/sepak-bola/arsip/{YYYY-MM-DD}
            urls.append(f"https://www.antaranews.com/sepak-bola/arsip/{iso}")
        elif "sindo" in p_lower:
            # Sindonews Sports index: https://sports.sindonews.com/indeks/0?t={YYYY-MM-DD}
            urls.append(f"https://sports.sindonews.com/indeks/0?t={iso}")
        elif "kompas" in p_lower:
            # Kompas Bola index: https://bola.kompas.com/search/{YYYY-MM-DD}
            urls.append(f"https://bola.kompas.com/search/{iso}")
        else:
            # Generic fallback
            urls.append(f"https://sport.detik.com/sepakbola/indeks?date={m}%2F{d}%2F{y}")

        curr += timedelta(days=1)

    return urls


def parse_archive_index_html(
    html: str,
    portal: str,
    archive_date: str = ""
) -> List[Dict[str, str]]:
    """
    Extracts candidate article links from Indonesian sports portal date archive index HTML.
    Supports Detik, Antara, Kompas, Sindonews, and Bola.net index layouts.
    """
    candidates: List[Dict[str, str]] = []
    if not html:
        return candidates

    p_lower = portal.lower().strip()
    seen_urls: Set[str] = set()

    # Generic href extractor: <a ... href="..." ...>
    raw_links = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html, flags=re.DOTALL | re.IGNORECASE)

    for href, anchor_text in raw_links:
        href_clean = href.strip()
        anchor_clean = re.sub(r"<[^>]+>", " ", anchor_text).strip()
        anchor_clean = re.sub(r"\s+", " ", anchor_clean)

        is_match = False
        final_url = href_clean

        if "detik" in p_lower:
            # Matches: https://sport.detik.com/sepakbola/.../d-12345/... or sport.detik.com/.../d-...
            if ("sport.detik.com/sepakbola" in href_clean or "sport.detik.com" in href_clean) and "/d-" in href_clean:
                is_match = True
        elif "antara" in p_lower:
            # Matches: antaranews.com/berita/... (in sports context)
            if "antaranews.com/berita/" in href_clean:
                # filter out non-news fragments
                if not any(x in href_clean for x in ["/foto/", "/video/", "/infografis/"]):
                    is_match = True
        elif "kompas" in p_lower:
            # Matches: bola.kompas.com/read/...
            if "bola.kompas.com/read/" in href_clean:
                is_match = True
        elif "sindo" in p_lower:
            # Matches: sports.sindonews.com/read/...
            if "sports.sindonews.com/read/" in href_clean:
                is_match = True
        elif "bolanet" in p_lower:
            # Matches: bola.net/...
            if "bola.net/" in href_clean and not href_clean.endswith(".net/"):
                is_match = True
        else:
            # Generic fallback: check if URL contains sports keywords and has article structure
            href_lower = href_clean.lower()
            if any(kw in href_lower for kw in DEFAULT_FOOTBALL_KEYWORDS):
                if any(x in href_lower for x in ["/read/", "/d-", "/berita/", "/artikel/"]):
                    is_match = True

        if is_match:
            norm_url = normalize_portal_url(final_url)
            if norm_url not in seen_urls:
                seen_urls.add(norm_url)
                candidates.append({
                    "url": norm_url,
                    "title": anchor_clean if len(anchor_clean) >= 10 else "",
                    "published_date": normalize_timestamp(archive_date) if archive_date else "",
                    "archive_date": archive_date,
                })

    return candidates


def generate_archive_page_url(base_archive_url: str, portal: str, page_num: int) -> str:
    """Generates paginated archive index URL for page > 1."""
    if page_num <= 1:
        return base_archive_url

    p_lower = portal.lower().strip()
    if "detik" in p_lower:
        sep = "&" if "?" in base_archive_url else "?"
        return f"{base_archive_url}{sep}page={page_num}"
    elif "sindo" in p_lower:
        sep = "&" if "?" in base_archive_url else "?"
        return f"{base_archive_url}{sep}p={page_num}"
    elif "kompas" in p_lower:
        return f"{base_archive_url.rstrip('/')}/{page_num}"
    elif "antara" in p_lower:
        return f"{base_archive_url.rstrip('/')}/{page_num}"
    else:
        sep = "&" if "?" in base_archive_url else "?"
        return f"{base_archive_url}{sep}page={page_num}"


def fetch_archive_articles(
    portal: str,
    start_date: str,
    end_date: str,
    max_articles: int = 1000,
    max_pages_per_date: int = 3
) -> List[Dict[str, str]]:
    """
    Crawls historical date archive pages for a portal between start_date and end_date.
    Includes sub-page pagination probing (pages 1..N) with early exit when no new items appear.
    Yields list of candidate article URLs.
    """
    archive_urls = generate_portal_archive_urls(portal, start_date, end_date)
    collected: List[Dict[str, str]] = []
    seen: Set[str] = set()

    logger.info(f"Generated {len(archive_urls)} archive date index pages for '{portal}' ({start_date} to {end_date})")

    for arch_url in archive_urls:
        if len(collected) >= max_articles:
            break

        # Extract archive date from URL if available
        date_match = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}%2F\d{2}%2F\d{4})", arch_url)
        arch_date_str = date_match.group(1) if date_match else ""

        # Probe pages for this date (page 1, 2, ... up to max_pages_per_date)
        for page_num in range(1, max_pages_per_date + 1):
            if len(collected) >= max_articles:
                break

            target_page_url = generate_archive_page_url(arch_url, portal=portal, page_num=page_num)

            try:
                status, body_bytes, _ = default_http_client.fetch(target_page_url, timeout=12.0)
                if status != 200 or not body_bytes:
                    break
                html_text = body_bytes.decode("utf-8", errors="replace")
            except Exception as e:
                logger.debug(f"Failed to fetch archive index {target_page_url}: {e}")
                break

            items = parse_archive_index_html(html_text, portal=portal, archive_date=arch_date_str)
            if not items:
                # No articles found on this sub-page -> stop pagination for this date
                break

            new_on_page = 0
            for item in items:
                u = item["url"]
                if u not in seen:
                    seen.add(u)
                    collected.append(item)
                    new_on_page += 1
                    if len(collected) >= max_articles:
                        break

            # Early exit: if no new unique articles added, or page had very few items, stop pagination
            if new_on_page == 0 or len(items) < 10:
                break

    return collected


def run_sitemap_backfill(
    portal: Optional[str] = None,
    sitemap_url: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    days: Optional[int] = None,
    mode: str = "both",
    resume: bool = False,
    output_file: str = "data/raw/news/football.jsonl",
    max_articles: int = 100,
    max_workers: int = 5,
    min_content_length: int = 150,
    frontier_db: str = DEFAULT_FRONTIER_DB
) -> Dict[str, Any]:
    """
    Orchestrates historical football article backfilling via sitemaps and date archives.
    Integrates persistent SQLite CrawlFrontier and thread-safe streaming disk append.
    mode: 'sitemap' | 'archive' | 'both'
    """
    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    frontier = CrawlFrontier(frontier_db)

    # 1. Handle resume mode: reset stuck in_progress tasks
    if resume:
        reset_cnt = frontier.reset_in_progress()
        logger.info(f"Resume active: reset {reset_cnt} previously in_progress URLs to pending.")

    # 2. Compute date range if days is passed
    target_portal = portal or "detik"
    if days and days > 0:
        now_dt = datetime.now(timezone.utc)
        end_date = now_dt.strftime("%Y-%m-%d")
        start_date = (now_dt - timedelta(days=days)).strftime("%Y-%m-%d")

    # 3. Discovery Phase: Populate Frontier (if not purely resuming)
    new_candidates_added = 0
    if not resume:
        # A. Sitemap discovery
        if mode in ("sitemap", "both"):
            target_sitemaps: List[str] = []
            if sitemap_url:
                target_sitemaps.append(sitemap_url)
            elif portal:
                p_key = portal.lower().strip()
                target_sitemaps.extend(KNOWN_PORTAL_SITEMAPS.get(p_key, []))
            else:
                for p in ["detik", "antara", "bolanet"]:
                    target_sitemaps.extend(KNOWN_PORTAL_SITEMAPS.get(p, []))

            for sm_url in target_sitemaps:
                discovered = fetch_sitemap_urls(
                    sm_url,
                    filter_keywords=DEFAULT_FOOTBALL_KEYWORDS,
                    max_urls=max_articles
                )
                added = frontier.add_candidates(discovered, portal=target_portal)
                new_candidates_added += added
                logger.info(f"Sitemap {sm_url}: added {added} new candidates to frontier")

        # B. Archive date index discovery
        if mode in ("archive", "both") and start_date and end_date:
            archive_items = fetch_archive_articles(
                portal=target_portal,
                start_date=start_date,
                end_date=end_date,
                max_articles=max_articles
            )
            added = frontier.add_candidates(archive_items, portal=target_portal)
            new_candidates_added += added
            logger.info(f"Archive index ({start_date} to {end_date}): added {added} new candidates to frontier")

    # 4. Ingestion / Extraction Phase from Persistent Frontier
    batch = frontier.get_pending_batch(limit=max_articles, portal=portal if portal else None)
    if not batch:
        summary = frontier.get_frontier_summary()
        return {
            "status": "success",
            "message": "No pending items in frontier queue.",
            "collected": 0,
            "new_candidates_added": new_candidates_added,
            "frontier_summary": summary,
            "output_file": str(out_path),
        }

    urls_to_process = [item["url"] for item in batch]
    frontier.mark_in_progress(urls_to_process)
    logger.info(f"Processing batch of {len(batch)} candidate articles via {max_workers} worker threads")

    # Thread-safe streaming file write
    file_lock = threading.Lock()
    written_count = 0
    channel = PortalNewsChannel()

    def _extract_and_stream_worker(cand: Dict[str, Any]) -> bool:
        nonlocal written_count
        u = cand["url"]
        try:
            res = channel.extract(u)
            if res.get("status") == "success":
                content = res.get("content", "").strip()
                if len(content) >= min_content_length:
                    record = {
                        "url": u,
                        "title": cand.get("title") or res.get("title") or "",
                        "published_date": cand.get("published_date") or res.get("published_date") or "",
                        "archive_date": cand.get("archive_date", ""),
                        "content": content,
                        "source": urlparse(u).netloc,
                        "backend": res.get("backend", "direct_http"),
                        "harvested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    line = json.dumps(record, ensure_ascii=False) + "\n"
                    with file_lock:
                        with open(out_path, "a", encoding="utf-8") as f:
                            f.write(line)
                            f.flush()
                        written_count += 1

                    frontier.mark_completed(u)
                    return True
                else:
                    frontier.mark_failed(u, error="Content length below minimum threshold")
            else:
                frontier.mark_failed(u, error=res.get("error", "Extraction failed"))
        except Exception as e:
            frontier.mark_failed(u, error=str(e))
        return False

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_extract_and_stream_worker, c) for c in batch]
        for f in as_completed(futures):
            pass

    summary = frontier.get_frontier_summary()
    return {
        "status": "success",
        "collected": written_count,
        "batch_size": len(batch),
        "new_candidates_added": new_candidates_added,
        "frontier_summary": summary,
        "output_file": str(out_path),
    }
