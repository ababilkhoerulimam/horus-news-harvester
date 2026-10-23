"""
Catalog-wide football sitemap & API discovery runner.
Discovers football article candidates across all 66 portals in config/portals_catalog.json
and streams newly found URLs directly into SQLite crawl_frontier.db.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Set
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from processors.crawl_frontier import CrawlFrontier, DEFAULT_FRONTIER_DB
from processors.http_client import default_http_client
from scripts.sitemap_backfill import (
    DEFAULT_FOOTBALL_KEYWORDS,
    fetch_sitemap_urls,
    fetch_wordpress_rest_articles,
    _matches_keywords,
    normalize_portal_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("catalog_discovery")


def run_catalog_discovery(
    catalog_file: str = "config/portals_catalog.json",
    fingerprints_file: str = "config/portal_fingerprints.json",
    frontier_db: str = DEFAULT_FRONTIER_DB,
    max_urls_per_portal: int = 5000,
) -> Dict[str, Any]:
    frontier = CrawlFrontier(frontier_db)

    cat_path = Path(catalog_file)
    if not cat_path.exists():
        logger.error(f"Catalog file {catalog_file} not found.")
        return {"status": "error", "message": f"{catalog_file} missing"}

    with open(cat_path, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    # Load fingerprints if present
    fingerprints_map: Dict[str, Dict[str, Any]] = {}
    fp_path = Path(fingerprints_file)
    if fp_path.exists():
        try:
            with open(fp_path, "r", encoding="utf-8") as f:
                fp_list = json.load(f)
                for item in fp_list:
                    fingerprints_map[item["domain"]] = item
        except Exception as e:
            logger.warning(f"Failed to load fingerprints: {e}")

    summary = frontier.get_frontier_summary()
    existing_portals = set(summary.get("portals", {}).keys())
    existing_counts = {p: data.get("total", 0) for p, data in summary.get("portals", {}).items()}

    portals = catalog.get("portals", [])
    logger.info(f"Loaded {len(portals)} total portals from catalog.")

    total_discovered_all = 0
    total_added_all = 0
    portal_stats: Dict[str, Dict[str, int]] = {}

    for portal_entry in portals:
        domain = portal_entry["domain"]
        p_name = domain.split(".")[0]
        # Avoid overriding well-known keys
        if "kompas" in domain:
            p_name = "kompas"
        elif "detik" in domain:
            p_name = "detik"
        elif "jawapos" in domain:
            p_name = "jawapos"
        elif "sindo" in domain:
            p_name = "sindonews"

        # If portal already has over 5000 items in frontier (like detik, kompas, jawapos), skip heavy discovery
        if existing_counts.get(p_name, 0) >= 5000:
            logger.info(f"Portal '{p_name}' already has {existing_counts[p_name]} items in frontier. Skipping.")
            continue

        logger.info(f"[*] Discovering football articles for: {domain} ({portal_entry.get('name')})")

        fp = fingerprints_map.get(domain, {})
        sitemaps_to_try: List[str] = []

        # 1. Fingerprint active sitemaps
        if fp.get("active_sitemaps"):
            sitemaps_to_try.extend(fp["active_sitemaps"])

        # 2. Standard sitemaps
        sports_url = portal_entry.get("sports_url") or f"https://{domain}"
        parsed_sports = urlparse(sports_url)
        sports_host = f"{parsed_sports.scheme}://{parsed_sports.netloc}"

        candidates = [
            f"{sports_host}/sitemap.xml",
            f"{sports_host}/sitemap_news.xml",
            f"{sports_host}/post-sitemap.xml",
            f"https://{domain}/sitemap.xml",
            f"https://{domain}/sitemap_news.xml",
            f"https://{domain}/post-sitemap.xml",
        ]
        for c in candidates:
            if c not in sitemaps_to_try:
                sitemaps_to_try.append(c)

        # 3. WordPress REST API
        wp_endpoint = fp.get("wp_json_endpoint")
        discovered_articles: List[Dict[str, str]] = []

        if fp.get("has_wp_json") and wp_endpoint:
            try:
                wp_items = fetch_wordpress_rest_articles(
                    portal=p_name,
                    base_api_url=wp_endpoint,
                    start_date="2025-10-19",
                    end_date="2026-10-19",
                    max_articles=max_urls_per_portal,
                )
                discovered_articles.extend(wp_items)
                logger.info(f"WP-REST {domain}: found {len(wp_items)} articles")
            except Exception as e:
                logger.debug(f"WP-REST error for {domain}: {e}")

        # 4. Fetch sitemaps
        for sm in sitemaps_to_try:
            try:
                sm_items = fetch_sitemap_urls(
                    sm,
                    filter_keywords=DEFAULT_FOOTBALL_KEYWORDS,
                    max_urls=max_urls_per_portal,
                    max_depth=2,
                )
                discovered_articles.extend(sm_items)
            except Exception as e:
                logger.debug(f"Sitemap error for {sm}: {e}")

        # 5. Direct desk scraping for portals with no/limited sitemaps (e.g. Antara, CNN, Tempo, Kumparan)
        if len(discovered_articles) < 10 and portal_entry.get("has_sports_desk"):
            try:
                desk_url = portal_entry.get("sports_url") or f"https://{domain}"
                status, body_bytes, _ = default_http_client.fetch(desk_url, timeout=10.0)
                if status == 200 and body_bytes:
                    html_txt = body_bytes.decode("utf-8", errors="replace")
                    raw_links = re.findall(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html_txt, flags=re.DOTALL | re.IGNORECASE)
                    for href, anchor in raw_links:
                        href_clean = href.strip()
                        anchor_clean = re.sub(r"<[^>]+>", " ", anchor).strip()
                        if _matches_keywords(href_clean.lower(), anchor_clean.lower(), DEFAULT_FOOTBALL_KEYWORDS):
                            if any(x in href_clean.lower() for x in ["/read/", "/berita/", "/artikel/", "/post/", "/news/"]):
                                if href_clean.startswith("/"):
                                    href_clean = f"{sports_host}{href_clean}"
                                discovered_articles.append({
                                    "url": normalize_portal_url(href_clean),
                                    "title": anchor_clean if len(anchor_clean) >= 10 else "",
                                    "published_date": "",
                                    "archive_date": "",
                                })
            except Exception as e:
                logger.debug(f"Direct desk scrape error for {domain}: {e}")

        # Deduplicate within this portal run
        seen = set()
        unique_articles = []
        for a in discovered_articles:
            u = a["url"]
            if u not in seen:
                seen.add(u)
                unique_articles.append(a)

        added = frontier.add_candidates(unique_articles, portal=p_name)
        total_discovered_all += len(unique_articles)
        total_added_all += added

        portal_stats[p_name] = {
            "discovered": len(unique_articles),
            "new_queued": added,
        }
        print(f"[+] {p_name} ({domain}): discovered {len(unique_articles)} items ({added} new candidates queued in frontier)")

    return {
        "status": "success",
        "total_portals_scanned": len(portals),
        "total_discovered": total_discovered_all,
        "total_new_queued": total_added_all,
        "portal_stats": portal_stats,
    }


if __name__ == "__main__":
    res = run_catalog_discovery()
    print("\n=== Discovery Summary across 66 Portals ===")
    print(f"Total Discovered: {res['total_discovered']:,}")
    print(f"Total New Queued: {res['total_new_queued']:,}")
