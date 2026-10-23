"""
Topic-Agnostic National News Intelligence Harvester.
Harvests historical and live articles for any configured topic (e.g. Karhutla, IKN, Pemilu, etc.)
Leverages curl_cffi TLS impersonation, sitemaps, WP-REST APIs, and SQLite WAL frontier.
"""

from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Set, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed

# Ensure repository root is on sys.path
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml

from backends.curl_impersonate import CurlImpersonateBackend
from channels.portal_news import PortalNewsChannel, normalize_portal_url, extract_clean_article_text
from processors.cleaner import clean_news_text
from processors.crawl_frontier import CrawlFrontier
from processors.http_client import default_http_client, DEFAULT_PROXIES_FILE
from scripts.sitemap_backfill import fetch_sitemap_urls, fetch_wordpress_rest_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("topic_harvester")


def load_topic_config(topic_name: str) -> Dict[str, Any]:
    """Loads and validates a topic configuration YAML file."""
    topic_path = Path("config/topics") / f"{topic_name}.yaml"
    if not topic_path.exists():
        raise FileNotFoundError(f"Topic config not found: {topic_path}")

    with open(topic_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return cfg


def extract_topic_keywords(cfg: Dict[str, Any]) -> List[str]:
    """Extracts a flat, deduplicated list of lowercase keywords from topic config."""
    kws = set()
    raw_kw = cfg.get("keywords", {})
    if isinstance(raw_kw, list):
        for k in raw_kw:
            kws.add(str(k).lower().strip())
    elif isinstance(raw_kw, dict):
        for k, v in raw_kw.items():
            if isinstance(v, list):
                for item in v:
                    kws.add(str(item).lower().strip())
            elif isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    if isinstance(sub_v, list):
                        for item in sub_v:
                            kws.add(str(item).lower().strip())
    return sorted(list(kws))


def discover_topic_articles(
    cfg: Dict[str, Any],
    frontier: CrawlFrontier,
    days: int = 365,
    limit_per_portal: int = 5000,
) -> int:
    """
    Discovers candidate articles matching topic keywords across targeted portals.
    Queues discovered URLs into the topic's frontier database.
    """
    topic_id = cfg.get("topic", {}).get("id", "general")
    keywords = extract_topic_keywords(cfg)
    logger.info(f"[*] Discovering candidates for topic '{topic_id}' with {len(keywords)} keywords")

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    end_date_str = now.strftime("%Y-%m-%d")

    total_discovered = 0
    target_portals = cfg.get("target_portals", {})

    flat_portals = []
    for cat_name, portal_list in target_portals.items():
        if isinstance(portal_list, list):
            for p in portal_list:
                flat_portals.append(p)

    for p in flat_portals:
        domain = p.get("domain", "")
        name = p.get("name", domain)
        adapter = p.get("adapter", "sitemap")
        portal_id = domain.replace(".", "_")

        logger.info(f"[*] Probing portal: {name} ({domain}) via {adapter}")
        portal_items = []

        if adapter == "wordpress_rest":
            wp_base = p.get("wp_api_base") or f"https://{domain}/wp-json/wp/v2"
            try:
                wp_articles = fetch_wordpress_rest_articles(
                    portal=portal_id,
                    base_api_url=wp_base,
                    start_date=start_date_str,
                    end_date=end_date_str,
                    max_articles=limit_per_portal,
                    filter_keywords=keywords,
                )
                for a in wp_articles:
                    portal_items.append({"url": a["url"], "published_date": a.get("published_date", "")})
            except Exception as e:
                logger.warning(f"[-] WP-REST failed for {domain}: {e}")

        elif adapter == "sitemap":
            sitemap_url = p.get("sitemap_url") or f"https://{domain}/sitemap.xml"
            try:
                sm_items = fetch_sitemap_urls(
                    sitemap_url,
                    filter_keywords=keywords,
                    max_urls=limit_per_portal,
                )
                portal_items.extend(sm_items)
            except Exception as e:
                logger.warning(f"[-] Sitemap failed for {domain}: {e}")

        if portal_items:
            queued = frontier.add_candidates(portal_items, portal=portal_id)
            total_discovered += queued
            logger.info(f"[+] {portal_id}: discovered {len(portal_items)} items ({queued} new queued)")
        else:
            logger.info(f"[-] {portal_id}: 0 items found")

    return total_discovered


def harvest_topic(
    topic_name: str,
    days: int = 365,
    workers: int = 16,
    limit: int = 100000,
    resume: bool = False,
    stats_only: bool = False,
):
    """Orchestrates topic discovery and concurrent extraction to JSONL."""
    cfg = load_topic_config(topic_name)
    topic_id = cfg.get("topic", {}).get("id", topic_name)
    output_file = cfg.get("topic", {}).get("default_output", f"data/raw/news/{topic_id}.jsonl")
    db_path = f"data/state/crawl_{topic_id}_frontier.db"

    frontier = CrawlFrontier(db_path=db_path)

    if DEFAULT_PROXIES_FILE.exists():
        loaded = default_http_client.load_proxies_from_file(str(DEFAULT_PROXIES_FILE))
        if loaded:
            logger.info(f"[*] Auto-loaded {loaded} proxies from '{DEFAULT_PROXIES_FILE}'")

    if stats_only:
        summary = frontier.get_frontier_summary()
        totals = summary["totals"]
        print(f"\n=== Crawl Frontier Stats: Topic [{topic_id}] ===")
        print(f"Total Discovered: {totals['total']:,}")
        print(f"  - Completed:    {totals['completed']:,}")
        print(f"  - Pending:      {totals['pending']:,}")
        print(f"  - In Progress:  {totals['in_progress']:,}")
        print(f"  - Failed:       {totals['failed']:,}")
        print("\nBreakdown by Portal:")
        for portal, pstats in summary["portals"].items():
            print(f"  * {portal}: {pstats['total']} total ({pstats['completed']} done, {pstats['pending']} pending, {pstats['failed']} failed)")
        print("================================================")
        return

    if not resume:
        logger.info(f"[*] Starting discovery phase for topic '{topic_id}' (past {days} days)...")
        discover_topic_articles(cfg, frontier, days=days)

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    channel = PortalNewsChannel()
    curl_backend = CurlImpersonateBackend()

    logger.info(f"[*] Starting extraction with {workers} workers into '{out_path}'...")
    processed_count = 0

    def extract_task(item: Dict[str, Any]) -> Tuple[str, bool, Optional[Dict[str, Any]], str]:
        raw_url = item["url"]
        norm_url = normalize_portal_url(raw_url)
        portal = item.get("portal", "unknown")

        try:
            # 1. Primary: curl_cffi browser TLS impersonation
            if curl_backend.is_available():
                proxy_endpoint = default_http_client.get_active_proxy()
                status, html = curl_backend.fetch(norm_url, proxy=proxy_endpoint)
                if status == 200 and html:
                    clean_content = extract_clean_article_text(html)
                    if clean_content and len(clean_content) >= 120:
                        doc = {
                            "url": norm_url,
                            "title": item.get("title", ""),
                            "content": clean_content,
                            "topic": topic_id,
                            "portal": portal,
                            "publish_date": item.get("published_date", ""),
                            "harvested_at": datetime.now(timezone.utc).isoformat(),
                        }
                        return norm_url, True, doc, ""

            # 2. Secondary fallback: portal_news channel pipeline
            res = channel.extract(norm_url, config={"disable_headless": True})
            if res.get("status") == "success" and res.get("content"):
                doc = {
                    "url": norm_url,
                    "title": res.get("title", item.get("title", "")),
                    "content": res.get("content", ""),
                    "topic": topic_id,
                    "portal": portal,
                    "publish_date": item.get("published_date", ""),
                    "harvested_at": datetime.now(timezone.utc).isoformat(),
                }
                return norm_url, True, doc, ""

            return norm_url, False, None, "Content length below threshold"
        except Exception as e:
            return norm_url, False, None, str(e)

    with open(out_path, "a", encoding="utf-8") as out_f:
        while processed_count < limit:
            batch = frontier.get_pending_batch(limit=min(workers * 4, 100))
            if not batch:
                logger.info("[*] Frontier queue empty. Topic harvest completed.")
                break

            batch_urls = [item["url"] for item in batch]
            frontier.mark_in_progress(batch_urls)

            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(extract_task, item) for item in batch]
                for fut in as_completed(futures):
                    norm_url, success, doc, err = fut.result()
                    if success and doc:
                        out_f.write(json.dumps(doc, ensure_ascii=False) + "\n")
                        out_f.flush()
                        frontier.mark_completed(norm_url)
                        processed_count += 1
                        if processed_count % 25 == 0:
                            logger.info(f"[+] Harvested {processed_count} articles -> {out_path.name}")
                    else:
                        frontier.mark_failed(norm_url, error=err)

    logger.info(f"[*] Topic '{topic_id}' harvest session finished. Total extracted: {processed_count}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Topic-Agnostic National News Intelligence Harvester")
    parser.add_argument("--topic", "-t", default="karhutla", help="Topic ID (e.g. karhutla)")
    parser.add_argument("--days", "-d", type=int, default=365, help="Past days to crawl (default: 365)")
    parser.add_argument("--workers", "-w", type=int, default=16, help="Worker threads (default: 16)")
    parser.add_argument("--limit", "-l", type=int, default=100000, help="Article harvest limit")
    parser.add_argument("--resume", "-r", action="store_true", help="Resume pending frontier queue")
    parser.add_argument("--stats", action="store_true", help="Display topic frontier stats")

    args = parser.parse_args()
    harvest_topic(
        topic_name=args.topic,
        days=args.days,
        workers=args.workers,
        limit=args.limit,
        resume=args.resume,
        stats_only=args.stats,
    )
