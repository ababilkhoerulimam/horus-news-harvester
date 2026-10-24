"""
Historical Sitemap & Date Archive Crawler for Indonesian Sports Portals.
Enables large-scale backfilling (10k - 100k articles) for NLP Domain-Adaptive
Pre-Training (DAPT) and Named Entity Recognition (NER).
"""
from __future__ import annotations

import json
import gzip
import logging
import multiprocessing
from multiprocessing import Process
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
import asyncio

# Ensure repository root is on Python module search path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backends.curl_impersonate import CurlImpersonateBackend
from channels.portal_news import (
    PortalNewsChannel,
    normalize_portal_url,
    extract_clean_article_text,
    is_cloudflare_challenge,
)
from processors.ad_filter import is_ad_domain
from processors.cleaner import clean_news_text, normalize_timestamp
from processors.crawl_frontier import CrawlFrontier, DEFAULT_FRONTIER_DB, interleave_by_portal
from processors.http_client import default_http_client

logger = logging.getLogger("sitemap_backfill")

# Default football-specific path and title keyword filters (Exhaustive Indonesian & International taxonomy)
DEFAULT_FOOTBALL_KEYWORDS = [
    # 1. Core General Football Terms
    "sepakbola", "sepak-bola", "sepak bola", "football", "soccer", "futsal", "bola",

    # 2. Indonesian Leagues & Tournaments
    "liga 1", "liga 2", "liga 3", "liga-1", "liga-2", "liga-3",
    "liga indonesia", "liga-indonesia", "bri liga 1", "bri-liga-1", "shopee-liga-1",
    "pegadaian liga 2", "pegadaian-liga-2", "piala presiden", "piala-presiden",
    "piala indonesia", "piala-indonesia", "epa liga 1", "elite pro academy", "soeratin", "piala soeratin",

    # 3. Indonesian Football Governance
    "pssi", "pt lib", "pt-lib", "operator liga", "komdis pssi", "exco pssi",

    # 4. Indonesian National Team (Timnas)
    "timnas", "timnas indonesia", "timnas-indonesia", "skuad garuda", "garuda muda",
    "garuda pertiwi", "timnas senior", "timnas u23", "timnas u20", "timnas u17",
    "timnas u-23", "timnas u-20", "timnas u-17", "timnas putri",

    # 5. Indonesian Coaches, Managers, Pundits
    "shin tae-yong", "shin tae yong", "sty", "erick thohir", "erick-thohir",
    "indra sjafri", "nova arianto", "coach justin", "bung ropan", "bung binder",
    "bojan hodak", "carlos pena", "gilbert agius", "pieter huistra", "paul munster",
    "stefano cugurra", "teco", "bernardo tavares",

    # 6. Indonesian National Team & Star League Players
    "jay idzes", "thom haye", "maarten paes", "marselino ferdinan", "marselino",
    "rafael struick", "ragnar oratmangoen", "nathan tjoe-a-on", "nathan", "calvin verdonk", "verdonk",
    "rizky ridho", "asnawi mangkualam", "asnawi", "pratama arhan", "arhan", "justin hubner",
    "sandy walsh", "jordi amat", "ivar jenner", "shayne pattynama", "eliano reijnders",
    "mees hilgers", "ernando ari", "nadeo argawinata", "dimas drajad", "hokky caraka",
    "egy maulana vikri", "egy maulana", "witan sulaeman", "ricky kambuaya", "marc klok",
    "yakob sayuri", "yance sayuri", "wahyu prasetyo", "malik risaldi", "ramadhan sananta",
    "beckham putra", "david da silva", "ciro alves", "gustavo almeida", "alex martins",

    # 7. Indonesian Liga 1 Clubs & Fan Monikers
    "persib", "maung bandung", "pangeran biru",
    "persija", "macan kemayoran", "the jakmania", "jakmania",
    "persebaya", "bajul ijo", "bonek",
    "arema", "singo edan", "aremania",
    "bali united", "bali-united", "serdadu tridatu",
    "psm makassar", "psm-makassar", "jukueja",
    "borneo fc", "borneo-fc", "pesut etam",
    "dewa united", "dewa-united", "banten warrior",
    "madura united", "madura-united", "laskar sape kerrab",
    "psis semarang", "psis-semarang", "laskar mahesa jenar", "panser biru", "snex",
    "persis solo", "persis-solo", "laskar sambernyawa", "pasoepati",
    "persik kediri", "persik-kediri", "macan putih",
    "persita tangerang", "persita", "pendekar cisadane",
    "barito putera", "barito-putera", "laskar antasari",
    "pss sleman", "pss-sleman", "super elang jawa", "super elja", "bcs x pss", "slemania",
    "malut united", "malut-united", "laskar kie raha",
    "semen padang", "semen-padang", "kabau sirah",
    "psbs biak", "psbs-biak", "badai pasifik",

    # 8. Indonesian Liga 2 Heritage Clubs
    "sriwijaya fc", "sriwijaya-fc", "laskar wong kito",
    "psms medan", "psms-medan", "ayam kinantan",
    "persipura jayapura", "persipura", "mutiara hitam",
    "persela lamongan", "persela", "laskar joko tingkir",
    "deltras", "gresik united", "persijap jepara", "persijap", "persiraja",

    # 9. Asian & ASEAN Regional Tournaments
    "piala asia", "asian cup", "piala-asia", "afc", "kualifikasi piala asia",
    "piala aff", "piala-aff", "aff cup", "asean cup", "asean championship",
    "afc champions league", "acl two", "piala afc", "sea games",

    # 10. World Cup & Global Tournaments
    "piala dunia", "world cup", "world-cup", "piala-dunia", "fifa",
    "kualifikasi piala dunia", "kualifikasi-piala-dunia", "fifa matchday", "fifa match day",

    # 11. Foreign Leagues Heavily Covered in Indo Desks
    "champions league", "champions-league", "liga champions", "liga-champions", "uefa",
    "premier league", "premier-league", "liga inggris", "liga-inggris",
    "la liga", "la-liga", "liga spanyol", "liga-spanyol",
    "serie a", "serie-a", "liga italia", "liga-italia",
    "bundesliga", "ligue 1", "ligue-1", "eredivisie", "saudi pro league",

    # 12. Top Global Clubs Covered by Indo Portals
    "real madrid", "barcelona", "manchester united", "manchester city",
    "liverpool", "arsenal", "chelsea", "tottenham", "juventus",
    "ac milan", "inter milan", "bayern munchen", "bayern munich", "psg",
    "al nassr", "al hilal", "inter miami",
]


def _matches_keywords(url_lower: str, title_lower: str, keywords: List[str]) -> bool:
    """
    Checks if an article URL or headline matches any football keyword.
    Tolerates differences between hyphens, underscores, and spaces.
    """
    if not keywords:
        return True
    url_spaced = url_lower.replace("-", " ").replace("_", " ").replace("/", " ")
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in url_lower:
            return True
        if title_lower and kw_lower in title_lower:
            return True
        kw_spaced = kw_lower.replace("-", " ").replace("_", " ")
        if kw_spaced in url_spaced:
            return True
        if title_lower and kw_spaced in title_lower:
            return True
        kw_hyphen = kw_lower.replace(" ", "-")
        if kw_hyphen in url_lower:
            return True
    return False

# Known XML Sitemaps of Indonesian news portals
KNOWN_PORTAL_SITEMAPS: Dict[str, List[str]] = {
    "detik": [
        "https://sport.detik.com/sitemap_sepakbola.xml",
        "https://sport.detik.com/sitemap_news.xml",
        "https://sport.detik.com/sitemap.xml",
    ],
    "kompas": [
        "https://bola.kompas.com/sitemap.xml",
        "https://www.kompas.com/sitemap-news-sport.xml",
        "https://www.kompas.com/sitemap-archive-sport.xml",
    ],
    "bolasport": [
        "https://www.bolasport.com/sitemap-news.xml",
        "https://www.bolasport.com/sitemap-web.xml",
    ],
    "sindonews": [
        "https://sports.sindonews.com/sitemap.xml",
        "https://sports.sindonews.com/sitemap-news.xml",
    ],
    "suara": [
        "https://www.suara.com/sitemap.xml",
        "https://www.suara.com/news/sitemap-news.xml",
        "https://www.suara.com/news/sitemap-web.xml",
    ],
    "tribun": [
        "https://www.tribunnews.com/sitemap.xml",
        "https://www.tribunnews.com/nasional/sitemap-news.xml",
    ],
    "jawapos": [
        "https://www.jawapos.com/sitemap.xml",
        "https://www.jawapos.com/post-recent.xml",
    ],
    "skor": [
        "https://skor.id/sitemap.xml",
    ],
}

# Archive query endpoint configurations for 1-year historical crawling
ARCHIVE_PORTAL_CONFIGS: Dict[str, Dict[str, str]] = {
    "detik": {
        "url_tpl": "https://sport.detik.com/sepakbola/indeks?date={m}%2F{d}%2F{y}",
        "page_tpl": "{base}&page={page}",
    },
    "kompas": {
        "url_tpl": "https://bola.kompas.com/search/{iso}",
        "page_tpl": "{base}/{page}",
    },
    "okezone": {
        "url_tpl": "https://bola.okezone.com/indeks/{y}/{m}/{d}/",
        "page_tpl": "{base}/{offset}/",
    },
    "suara": {
        "url_tpl": "https://www.suara.com/indeks/terkini/bola?date={iso}",
        "page_tpl": "{base}&page={page}",
    },
}

# WordPress REST API portals with native date range query support
WORDPRESS_REST_PORTALS: Dict[str, str] = {
    "beritajatim": "https://beritajatim.com/wp-json/wp/v2/posts",
    "balipost": "https://www.balipost.com/wp-json/wp/v2/posts",
    "fajar": "https://fajar.co.id/wp-json/wp/v2/posts",
    "bantennews": "https://www.bantennews.co.id/wp-json/wp/v2/posts",
    "niaga_asia": "https://www.niaga.asia/wp-json/wp/v2/posts",
}

NON_FOOTBALL_SPORTS = [
    "bola voli", "voli", "volleyball",
    "bola basket", "basket", "basketball",
    "bulutangkis", "badminton",
    "motogp", "f1", "formula 1",
    "tenis", "tennis",
]


def _decompress_sitemap_bytes(data: bytes) -> str:
    """
    Auto-detects and decompresses gzip-encoded sitemap payloads (magic header 1f 8b).
    Inspired by Scrapling sitemap decompression logic.
    """
    if len(data) >= 2 and data[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(data).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Gzip decompression failed: {e}")
    return data.decode("utf-8", errors="replace")


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

        is_match = _matches_keywords(url_lower, title_lower, keywords)

        if is_match and not is_ad_domain(raw_url):
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
        title_lower = title.lower()
        if _matches_keywords(url_lower, title_lower, keywords):
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

    while to_visit and (max_urls <= 0 or len(collected_articles) < max_urls):
        current_url, depth = to_visit.pop(0)
        if current_url in visited_sitemaps:
            continue
        visited_sitemaps.add(current_url)

        try:
            status, body_bytes, _ = default_http_client.fetch(current_url, timeout=15)
            if status != 200 or not body_bytes:
                continue
            xml_text = _decompress_sitemap_bytes(body_bytes)
        except Exception as e:
            logger.warning(f"Failed to fetch sitemap {current_url}: {e}")
            continue

        articles, child_sitemaps = parse_sitemap_xml(xml_text, filter_keywords=keywords)

        for art in articles:
            u = art["url"]
            if u not in seen_urls:
                seen_urls.add(u)
                collected_articles.append(art)
                if max_urls > 0 and len(collected_articles) >= max_urls:
                    break

        # Queue child sitemaps if depth allows
        if depth < max_depth:
            for child in child_sitemaps:
                child_lower = child.lower()
                if any(x in child_lower for x in ["image", "video", "foto", "tag-", "category-", "author-"]):
                    continue
                if any(k in child_lower for k in ["sport", "bola", "sepakbola", "news", "post", "artikel", "berita"]):
                    if child not in visited_sitemaps:
                        to_visit.append((child, depth + 1))
                elif not any(x in child_lower for x in ["lifestyle", "otomotif", "horoskop", "zodiak"]):
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
    cfg = ARCHIVE_PORTAL_CONFIGS.get(p_lower)

    while curr <= dt_end:
        y = curr.strftime("%Y")
        m = curr.strftime("%m")
        d = curr.strftime("%d")
        iso = curr.strftime("%Y-%m-%d")

        if cfg and "url_tpl" in cfg:
            urls.append(cfg["url_tpl"].format(base=cfg.get("base", ""), y=y, m=m, d=d, iso=iso))
        elif "detik" in p_lower:
            urls.append(f"https://sport.detik.com/sepakbola/indeks?date={m}%2F{d}%2F{y}")
        elif "antara" in p_lower:
            urls.append(f"https://www.antaranews.com/sepak-bola/arsip/{iso}")
        elif "sindo" in p_lower:
            urls.append(f"https://sports.sindonews.com/indeks/0?t={iso}")
        elif "kompas" in p_lower:
            urls.append(f"https://bola.kompas.com/search/{iso}")
        else:
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
        elif "okezone" in p_lower:
            # Matches: bola.okezone.com/read/...
            if "bola.okezone.com/read/" in href_clean:
                is_match = True
        elif "suara" in p_lower:
            # Matches: suara.com/bola/YYYY/MM/DD/...
            if "suara.com/bola/" in href_clean and re.search(r"/\d{4}/\d{2}/\d{2}/\d+/", href_clean):
                is_match = True
        elif "republika" in p_lower:
            # Matches: sport.republika.co.id/berita/...
            if ("sport.republika.co.id/berita/" in href_clean or "republika.co.id/berita/" in href_clean) and not any(x in href_clean for x in ["/foto/", "/video/"]):
                is_match = True
        elif "bolanet" in p_lower:
            # Matches: bola.net/...
            if "bola.net/" in href_clean and not href_clean.endswith(".net/"):
                is_match = True
        else:
            # Generic fallback: check if URL or anchor contains football keywords and has article structure
            href_lower = href_clean.lower()
            if _matches_keywords(href_lower, anchor_clean.lower(), DEFAULT_FOOTBALL_KEYWORDS):
                if any(x in href_lower for x in ["/read/", "/d-", "/berita/", "/artikel/", "/post/", "/news/"]):
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
    cfg = ARCHIVE_PORTAL_CONFIGS.get(p_lower)
    if cfg and "page_tpl" in cfg:
        offset = (page_num - 1) * 20
        return cfg["page_tpl"].format(base=base_archive_url.rstrip('/'), page=page_num, offset=offset)

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
        if max_articles > 0 and len(collected) >= max_articles:
            break

        # Extract archive date from URL if available
        date_match = re.search(r"(\d{4}[-/]\d{2}[-/]\d{2}|\d{2}%2F\d{2}%2F\d{4})", arch_url)
        arch_date_str = date_match.group(1) if date_match else ""

        # Probe pages for this date (page 1, 2, ... up to max_pages_per_date)
        for page_num in range(1, max_pages_per_date + 1):
            if max_articles > 0 and len(collected) >= max_articles:
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
                    if max_articles > 0 and len(collected) >= max_articles:
                        break

            # Early exit: if no new unique articles added on this page, stop pagination
            if new_on_page == 0:
                break

    return collected


def fetch_wordpress_rest_articles(
    portal: str,
    base_api_url: str,
    start_date: str,
    end_date: str,
    max_articles: int = 1000,
    filter_keywords: Optional[List[str]] = None,
) -> List[Dict[str, str]]:
    """
    Crawls historical articles from WordPress REST API endpoint between start_date and end_date.
    Filters candidate articles strictly by football keywords, excluding non-football sports.
    """
    keywords = [k.lower() for k in (filter_keywords or DEFAULT_FOOTBALL_KEYWORDS)]
    collected: List[Dict[str, str]] = []
    seen: Set[str] = set()
    page = 1
    max_pages = 50

    logger.info(f"Querying WordPress REST API for '{portal}' ({start_date} to {end_date})")

    while (max_articles <= 0 or len(collected) < max_articles) and page <= max_pages:
        endpoint = (
            f"{base_api_url}?after={start_date}T00:00:00&before={end_date}T23:59:59"
            f"&per_page=100&page={page}"
        )
        try:
            status, body_bytes, _ = default_http_client.fetch(endpoint, timeout=12.0)
            if status != 200 or not body_bytes:
                break
            posts = json.loads(body_bytes.decode("utf-8", errors="replace"))
            if not isinstance(posts, list) or not posts:
                break
        except Exception as e:
            logger.debug(f"WP-REST fetch error for {portal} page {page}: {e}")
            break

        for post in posts:
            link = post.get("link", "").strip()
            if not link or link in seen:
                continue

            title = post.get("title", {}).get("rendered", "")
            title_clean = re.sub(r"<[^>]+>", "", title).strip()
            content_sample = post.get("content", {}).get("rendered", "")[:500]

            text_to_check = f"{link} {title_clean} {content_sample}".lower()

            # Exclude non-football sports explicitly only when harvesting default football topic
            if not filter_keywords and any(nf in text_to_check for nf in NON_FOOTBALL_SPORTS) and not any(
                fb in text_to_check for fb in ["sepakbola", "sepak bola", "liga 1", "timnas", "pssi", "persib", "persija"]
            ):
                continue

            # Football keyword match
            if any(kw in text_to_check for kw in keywords):
                seen.add(link)
                pub_date = post.get("date", "")
                collected.append({
                    "url": normalize_portal_url(link),
                    "title": title_clean,
                    "published_date": normalize_timestamp(pub_date),
                    "archive_date": pub_date[:10] if len(pub_date) >= 10 else "",
                })
                if max_articles > 0 and len(collected) >= max_articles:
                    break

        page += 1

    return collected


async def _async_backfill_worker_loop(
    worker_id: int,
    output_part_file: str,
    frontier_db: str,
    concurrency: int = 30,
    limit_per_worker: int = 50000,
    min_content_length: int = 150,
    portal_filter: Optional[str] = None,
):
    """Event loop for one CPU core backfill process, streaming requests via AsyncQueue (direct-first)."""
    curl_backend = CurlImpersonateBackend()
    channel = PortalNewsChannel()
    frontier = CrawlFrontier(frontier_db, batch_size=50)
    session = curl_backend.create_async_session()
    extracted_total = 0

    part_path = Path(output_part_file)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    file_lock = asyncio.Lock()
    db_lock = asyncio.Lock()

    async def _fetch_single(cand: Dict[str, Any], out_handle):
        nonlocal extracted_total
        u = cand["url"]
        norm_u = normalize_portal_url(u)
        try:
            # 1. Primary: Direct fetch (fast, ~0.15s)
            status, html = await curl_backend.fetch_async(session, norm_u, proxy=None)

            # Fast-fail on HTTP 404: page deleted or non-existent on origin
            if status == 404:
                async with db_lock:
                    frontier.mark_failed(u, error="HTTP 404 Not Found")
                return

            # Fallback to rotating proxy if WAF/blocked/challenge
            if status in (0, 202, 403, 429, 503) or not html or is_cloudflare_challenge(status, html):
                proxy = default_http_client.get_active_proxy()
                if proxy:
                    status, html = await curl_backend.fetch_async(session, norm_u, proxy=proxy)

            if status == 200 and html and not is_cloudflare_challenge(status, html):
                content = extract_clean_article_text(html)
                if len(content) >= min_content_length:
                    record = {
                        "url": u,
                        "title": cand.get("title") or "",
                        "published_date": cand.get("published_date") or "",
                        "archive_date": cand.get("archive_date", ""),
                        "content": content,
                        "source": urlparse(u).netloc,
                        "backend": "curl_impersonate",
                        "harvested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    async with file_lock:
                        out_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        extracted_total += 1
                        curr = extracted_total

                    async with db_lock:
                        frontier.mark_completed(u)

                    if curr % 50 == 0:
                        async with file_lock:
                            out_handle.flush()
                        async with db_lock:
                            frontier.flush()
                        logger.info(f"[Core-{worker_id}] Harvested {curr} articles -> {part_path.name}")
                    return

            # 2. Secondary fallback via thread pool (for complex CMS or fallback)
            res = await asyncio.to_thread(channel.extract, u, {"disable_headless": True})
            if res.get("status") == "success" and len(res.get("content", "")) >= min_content_length:
                record = {
                    "url": u,
                    "title": cand.get("title") or res.get("title") or "",
                    "published_date": cand.get("published_date") or res.get("published_date") or "",
                    "archive_date": cand.get("archive_date", ""),
                    "content": res.get("content", ""),
                    "source": urlparse(u).netloc,
                    "backend": res.get("backend", "direct_http"),
                    "harvested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                }
                async with file_lock:
                    out_handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    extracted_total += 1
                    curr = extracted_total

                async with db_lock:
                    frontier.mark_completed(u)

                if curr % 50 == 0:
                    async with file_lock:
                        out_handle.flush()
                    async with db_lock:
                        frontier.flush()
                    logger.info(f"[Core-{worker_id}] Harvested {curr} articles -> {part_path.name}")
                return

            async with db_lock:
                frontier.mark_failed(u, error="Content length below minimum threshold")
        except Exception as e:
            async with db_lock:
                frontier.mark_failed(u, error=str(e))

    try:
        with open(part_path, "a", encoding="utf-8") as out_f:
            while extracted_total < limit_per_worker:
                if worker_id > 0:
                    await asyncio.sleep(0.05 * worker_id)

                batch = frontier.claim_pending_batch(limit=concurrency * 4, portal=portal_filter)
                if not batch:
                    break

                if not portal_filter:
                    batch = interleave_by_portal(batch)

                queue: asyncio.Queue = asyncio.Queue()
                for item in batch:
                    queue.put_nowait(item)

                async def _worker():
                    while not queue.empty():
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        await _fetch_single(item, out_f)
                        queue.task_done()

                worker_tasks = [asyncio.create_task(_worker()) for _ in range(concurrency)]
                await asyncio.gather(*worker_tasks)
                out_f.flush()
                frontier.flush()
                logger.info(f"[Core-{worker_id}] Batch completed | Extracted: {extracted_total} articles")
    finally:
        frontier.flush()
        frontier.close()
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass


def _backfill_worker_process_entrypoint(
    worker_id: int,
    output_part_file: str,
    frontier_db: str,
    concurrency: int = 30,
    limit_per_worker: int = 50000,
    min_content_length: int = 150,
    portal_filter: Optional[str] = None,
):
    """Top-level process entrypoint for backfill multiprocessing spawn on Windows."""
    asyncio.run(
        _async_backfill_worker_loop(
            worker_id=worker_id,
            output_part_file=output_part_file,
            frontier_db=frontier_db,
            concurrency=concurrency,
            limit_per_worker=limit_per_worker,
            min_content_length=min_content_length,
            portal_filter=portal_filter,
        )
    )


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
    frontier_db: str = DEFAULT_FRONTIER_DB,
    cores: int = 2,
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
            sitemaps_to_crawl: List[tuple[str, str]] = []  # (portal_name, sitemap_url)
            if sitemap_url:
                sitemaps_to_crawl.append((target_portal, sitemap_url))
            elif portal and portal.lower() != "all":
                p_key = portal.lower().strip()
                for sm in KNOWN_PORTAL_SITEMAPS.get(p_key, []):
                    sitemaps_to_crawl.append((p_key, sm))
            else:
                for p_name, urls in KNOWN_PORTAL_SITEMAPS.items():
                    for sm in urls:
                        sitemaps_to_crawl.append((p_name, sm))

            for p_name, sm_url in sitemaps_to_crawl:
                discovered = fetch_sitemap_urls(
                    sm_url,
                    filter_keywords=DEFAULT_FOOTBALL_KEYWORDS,
                    max_urls=max_articles
                )
                added = frontier.add_candidates(discovered, portal=p_name)
                new_candidates_added += added
                logger.info(f"Sitemap {p_name} ({sm_url}): added {added} new candidates to frontier")
                print(f"[+] Sitemap {p_name}: discovered {len(discovered)} items ({added} new candidates queued)")

        # B. Archive date index & WP-REST discovery
        if mode in ("archive", "both") and start_date and end_date:
            portals_to_archive = []
            if portal and portal.lower() == "all":
                portals_to_archive.extend(list(ARCHIVE_PORTAL_CONFIGS.keys()))
                portals_to_archive.extend(list(WORDPRESS_REST_PORTALS.keys()))
            elif portal:
                portals_to_archive.append(portal.lower().strip())
            else:
                portals_to_archive.append(target_portal)

            summary = frontier.get_frontier_summary()
            existing_counts = {p: p_data.get("total", 0) for p, p_data in summary.get("portals", {}).items()}

            for p_item in portals_to_archive:
                # In 'all' mode, skip re-crawling portals that already have thousands of items in frontier
                if portal and portal.lower() == "all" and existing_counts.get(p_item, 0) >= 5000:
                    print(f"[*] Archive {p_item}: already has {existing_counts[p_item]} items in frontier, skipping redundant index crawl.")
                    continue

                if p_item in WORDPRESS_REST_PORTALS:
                    wp_endpoint = WORDPRESS_REST_PORTALS[p_item]
                    wp_items = fetch_wordpress_rest_articles(
                        portal=p_item,
                        base_api_url=wp_endpoint,
                        start_date=start_date,
                        end_date=end_date,
                        max_articles=max_articles,
                    )
                    added = frontier.add_candidates(wp_items, portal=p_item)
                    new_candidates_added += added
                    logger.info(f"WP-REST ({p_item}): added {added} new candidates to frontier")
                    print(f"[+] WP-REST {p_item}: discovered {len(wp_items)} items ({added} new candidates queued)")
                else:
                    archive_items = fetch_archive_articles(
                        portal=p_item,
                        start_date=start_date,
                        end_date=end_date,
                        max_articles=max_articles,
                    )
                    added = frontier.add_candidates(archive_items, portal=p_item)
                    new_candidates_added += added
                    logger.info(f"Archive index {p_item} ({start_date} to {end_date}): added {added} new candidates to frontier")
                    print(f"[+] Archive {p_item}: discovered {len(archive_items)} items ({added} new candidates queued)")

    # 4. Ingestion / Extraction Phase from Persistent Frontier
    portal_filter = None if (not portal or portal.lower() == "all") else portal.lower().strip()
    batch_limit = max_articles if (max_articles and max_articles > 0) else 5000000
    actual_cores = max(1, cores)

    curl_backend = CurlImpersonateBackend()
    written_count = 0
    batch: List[Dict[str, Any]] = []

    if curl_backend.is_available() and actual_cores >= 2:
        concurrency = min(max_workers if max_workers > 0 else 30, 40)
        limit_per_core = max(1, batch_limit // actual_cores)
        logger.info(
            f"Starting {actual_cores}-Core Async Backfill ({actual_cores} cores x {concurrency} sockets) -> '{out_path}'"
        )

        processes: List[Process] = []
        part_files: List[Path] = []
        for i in range(actual_cores):
            p_file = Path(f"{output_file}.part{i}")
            part_files.append(p_file)
            p = Process(
                target=_backfill_worker_process_entrypoint,
                args=(i, str(p_file), frontier_db, concurrency, limit_per_core, min_content_length, portal_filter),
            )
            processes.append(p)
            p.start()

        for p in processes:
            p.join()

        merged_count = 0
        with open(out_path, "a", encoding="utf-8") as main_out:
            for p_file in part_files:
                if p_file.exists():
                    with open(p_file, "r", encoding="utf-8") as pf:
                        for line in pf:
                            if line.strip():
                                main_out.write(line)
                                merged_count += 1
                    try:
                        p_file.unlink()
                    except Exception:
                        pass
        written_count = merged_count
        logger.info(f"[+] Consolidated {merged_count} articles across {actual_cores} cores into '{out_path.name}'")

    else:
        batch = frontier.get_pending_batch(limit=batch_limit, portal=portal_filter)
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

        # Interleave batch across portals for balanced origin load and maximum throughput
        if not portal_filter:
            batch = interleave_by_portal(batch)

        urls_to_process = [item["url"] for item in batch]
        frontier.mark_in_progress(urls_to_process)
        channel = PortalNewsChannel()

        if curl_backend.is_available():
            # On Windows, keep socket concurrency within OS loopback limits (max 40) to prevent WinError 10054
            concurrency = min(max_workers if max_workers > 0 else 35, 40)
            logger.info(f"Processing batch of {len(batch)} candidate articles via AsyncQueue (concurrency: {concurrency}, direct-first)")

            async def _async_backfill_runner():
                nonlocal written_count
                session = curl_backend.create_async_session()
                queue: asyncio.Queue = asyncio.Queue()
                for cand in batch:
                    queue.put_nowait(cand)

                file_lock = asyncio.Lock()
                db_lock = asyncio.Lock()

                with open(out_path, "a", encoding="utf-8") as f:
                    async def _fetch_single(cand: Dict[str, Any]):
                        nonlocal written_count
                        u = cand["url"]
                        norm_u = normalize_portal_url(u)
                        try:
                            # 1. Primary: Direct fetch (fast, ~0.15s)
                            status, html = await curl_backend.fetch_async(session, norm_u, proxy=None)

                            # Fast-fail on HTTP 404: page deleted or non-existent on origin
                            if status == 404:
                                async with db_lock:
                                    frontier.mark_failed(u, error="HTTP 404 Not Found")
                                return

                            # Check if Cloudflare / rate limited / blocked -> Fallback to rotating proxy
                            if status in (0, 202, 403, 429, 503) or not html or is_cloudflare_challenge(status, html):
                                proxy = default_http_client.get_active_proxy()
                                if proxy:
                                    status, html = await curl_backend.fetch_async(session, norm_u, proxy=proxy)

                            if status == 200 and html and not is_cloudflare_challenge(status, html):
                                content = extract_clean_article_text(html)
                                if len(content) >= min_content_length:
                                    record = {
                                        "url": u,
                                        "title": cand.get("title") or "",
                                        "published_date": cand.get("published_date") or "",
                                        "archive_date": cand.get("archive_date", ""),
                                        "content": content,
                                        "source": urlparse(u).netloc,
                                        "backend": "curl_impersonate",
                                        "harvested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    }
                                    async with file_lock:
                                        f.write(json.dumps(record, ensure_ascii=False) + "\n")
                                        written_count += 1
                                        current_written = written_count

                                    async with db_lock:
                                        frontier.mark_completed(u)

                                    if current_written % 50 == 0:
                                        async with file_lock:
                                            f.flush()
                                        async with db_lock:
                                            frontier.flush()
                                        logger.info(f"[+] Harvested {current_written}/{len(batch)} candidate articles -> {out_path.name}")
                                    return

                            # 2. Secondary fallback via thread pool (for complex CMS or fallback)
                            res = await asyncio.to_thread(channel.extract, u, {"disable_headless": True})
                            if res.get("status") == "success" and len(res.get("content", "")) >= min_content_length:
                                record = {
                                    "url": u,
                                    "title": cand.get("title") or res.get("title") or "",
                                    "published_date": cand.get("published_date") or res.get("published_date") or "",
                                    "archive_date": cand.get("archive_date", ""),
                                    "content": res.get("content", ""),
                                    "source": urlparse(u).netloc,
                                    "backend": res.get("backend", "direct_http"),
                                    "harvested_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                }
                                async with file_lock:
                                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                                    written_count += 1
                                    current_written = written_count

                                async with db_lock:
                                    frontier.mark_completed(u)

                                if current_written % 50 == 0:
                                    async with file_lock:
                                        f.flush()
                                    async with db_lock:
                                        frontier.flush()
                                    logger.info(f"[+] Harvested {current_written}/{len(batch)} candidate articles -> {out_path.name}")
                                return

                            async with db_lock:
                                frontier.mark_failed(u, error="Content length below minimum threshold")
                        except Exception as e:
                            async with db_lock:
                                frontier.mark_failed(u, error=str(e))

                    async def _worker():
                        while not queue.empty():
                            try:
                                cand = queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            await _fetch_single(cand)
                            queue.task_done()

                    worker_tasks = [asyncio.create_task(_worker()) for _ in range(concurrency)]
                    await asyncio.gather(*worker_tasks)
                    f.flush()
                    frontier.flush()
                    logger.info(f"[+] Final batch flush | Total harvested: {written_count}/{len(batch)} articles -> {out_path.name}")

                if session:
                    await session.close()

            asyncio.run(_async_backfill_runner())

        else:
            logger.info(f"Processing batch of {len(batch)} candidate articles via {max_workers} worker threads")
            file_lock = threading.Lock()
            db_lock = threading.Lock()

            def _extract_and_stream_worker(cand: Dict[str, Any]) -> bool:
                nonlocal written_count
                u = cand["url"]
                try:
                    res = channel.extract(u, config={"disable_headless": True})
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

                            with db_lock:
                                frontier.mark_completed(u)
                            return True
                        else:
                            with db_lock:
                                frontier.mark_failed(u, error="Content length below minimum threshold")
                    else:
                        with db_lock:
                            frontier.mark_failed(u, error=res.get("error", "Extraction failed"))
                except Exception as e:
                    with db_lock:
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
