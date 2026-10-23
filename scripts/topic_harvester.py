"""
Topic-Agnostic National News Intelligence Harvester (NNIH).
Declarative crawling engine for Indonesian national intelligence topics.
Leverages Pydantic v2 validation, 66-portal preset auto-routing,
curl_cffi TLS JA4 impersonation, Inertia.js unpacking, and SQLite WAL frontier.
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
from processors.topic_config import TopicConfig, load_topic_config
from processors.portal_catalog import resolve_topic_portals, ResolvedPortal, CATALOG_PRESETS
from scripts.sitemap_backfill import fetch_sitemap_urls, fetch_wordpress_rest_articles

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("topic_harvester")


def generate_topic_template(topic_id: str, output_path: Optional[str] = None) -> Path:
    """Generates a clean, well-documented starter topic configuration YAML."""
    clean_id = topic_id.strip().lower().replace(" ", "_")
    target_path = Path(output_path) if output_path else Path("config/topics") / f"{clean_id}.yaml"
    target_path.parent.mkdir(parents=True, exist_ok=True)

    template_content = f"""# ==============================================================================
# Topic Configuration: {clean_id.upper()}
# National News Intelligence Harvester (NNIH)
# ==============================================================================

topic:
  id: "{clean_id}"
  name: "{clean_id.replace('_', ' ').title()}"
  description: "Intelligence monitoring and discourse analysis for {clean_id.replace('_', ' ')}"
  default_output: "data/raw/news/{clean_id}.jsonl"
  default_dapt_output: "data/processed/nusabert_{clean_id}_dapt.txt"

# ------------------------------------------------------------------------------
# Keywords Taxonomy (Flat list or nested category dictionary)
# ------------------------------------------------------------------------------
keywords:
  # Core events and phenomena
  events:
    - "{clean_id.replace('_', ' ')}"
    - "{clean_id.replace('_', '-')}"

  # Governance, regulatory, and policy actions
  policy:
    - "kebijakan"
    - "pemerintah"
    - "kementerian"

  # Geographic locations / focal regions
  focal_regions:
    - "indonesia"
    - "jakarta"

# ------------------------------------------------------------------------------
# Target Portals Configuration
# Built-in presets available:
#   * national_wire: 15 Tier-1 mainstream national media (Detik, Kompas, Antara, CNN, Tempo, etc.)
#   * regional_epicenters: Regional newspapers (Kalteng Pos, Riau Pos, Sumeks, BeritaJatim, etc.)
#   * investigative_environment: Investigative desks (Mongabay, Betahita, Forest Digest, Multatuli)
#   * all_66_catalog: All 66 fingerprinted national and regional news portals
# ------------------------------------------------------------------------------
portals:
  presets:
    - "national_wire"
    - "regional_epicenters"

  # Optional: Explicit additional portals to include
  include: []

  # Optional: Domains to blacklist from crawling
  exclude: []

# ------------------------------------------------------------------------------
# Output & Analytics Configuration
# ------------------------------------------------------------------------------
output:
  format: "jsonl"              # Options: jsonl, parquet, sqlite, duckdb
  min_word_count: 50           # Filter threshold for DAPT language modeling
"""
    with open(target_path, "w", encoding="utf-8") as f:
        f.write(template_content)

    logger.info(f"[+] Generated starter topic template at '{target_path}'")
    return target_path


def validate_topic(topic_name: str) -> Dict[str, Any]:
    """Inspects and validates a topic configuration, reporting stats and resolved surface."""
    cfg = load_topic_config(topic_name)
    keywords = cfg.get_keywords()
    resolved_portals = resolve_topic_portals(cfg)

    report = {
        "valid": True,
        "id": cfg.topic.id,
        "name": cfg.topic.name,
        "description": cfg.topic.description,
        "total_keywords": len(keywords),
        "sample_keywords": keywords[:10],
        "total_resolved_portals": len(resolved_portals),
        "resolved_portals_count": len(resolved_portals),
        "portals": [
            {
                "domain": p.domain,
                "name": p.name,
                "adapter": p.adapter,
                "endpoint": p.wp_api_base if p.adapter == "wordpress_rest" else p.sitemap_url,
                "region": p.region,
            }
            for p in resolved_portals
        ],
        "output_path": str(cfg.get_output_path()),
        "dapt_path": str(cfg.get_dapt_output_path()),
    }
    return report


def discover_topic_articles(
    cfg: TopicConfig,
    frontier: CrawlFrontier,
    days: int = 365,
    limit_per_portal: int = 5000,
) -> int:
    """
    Discovers candidate articles matching topic keywords across targeted portals.
    Queues discovered URLs into the topic's frontier database.
    """
    topic_id = cfg.topic.id
    keywords = cfg.get_keywords()
    resolved_portals = resolve_topic_portals(cfg)

    logger.info(f"[*] Discovering candidates for topic '{topic_id}' across {len(resolved_portals)} portals ({len(keywords)} keywords)")

    now = datetime.now(timezone.utc)
    start_dt = now - timedelta(days=days)
    start_date_str = start_dt.strftime("%Y-%m-%d")
    end_date_str = now.strftime("%Y-%m-%d")

    total_discovered = 0

    for p in resolved_portals:
        portal_id = p.portal_id
        logger.info(f"[*] Probing portal: {p.name} ({p.domain}) via {p.adapter}")
        portal_items = []

        if p.adapter == "wordpress_rest":
            wp_base = p.wp_api_base or f"https://{p.domain}/wp-json/wp/v2"
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
                logger.warning(f"[-] WP-REST failed for {p.domain}: {e}")

        elif p.adapter == "sitemap":
            sitemap_url = p.sitemap_url or f"https://{p.domain}/sitemap.xml"
            try:
                sm_items = fetch_sitemap_urls(
                    sitemap_url,
                    filter_keywords=keywords,
                    max_urls=limit_per_portal,
                )
                portal_items.extend(sm_items)
            except Exception as e:
                logger.warning(f"[-] Sitemap failed for {p.domain}: {e}")

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
    output_override: Optional[str] = None,
):
    """Orchestrates topic discovery and concurrent extraction to JSONL."""
    cfg = load_topic_config(topic_name)
    topic_id = cfg.topic.id
    output_file = output_override or str(cfg.get_output_path())
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

            # 2. Secondary fallback: portal_news channel pipeline (Inertia.js + Trafilatura)
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


def export_topic_dataset(
    topic_name: str,
    format_type: str = "parquet",
    out_path: Optional[str] = None
) -> Dict[str, Any]:
    """Exports harvested JSONL records into Parquet, SQLite, or DuckDB analytical formats."""
    cfg = load_topic_config(topic_name)
    input_file = cfg.get_output_path()
    if not input_file.exists():
        return {"status": "error", "message": f"Input file not found: {input_file}"}

    records = []
    with open(input_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                try:
                    records.append(json.loads(stripped))
                except Exception:
                    pass

    if not records:
        return {"status": "warn", "message": "No records to export"}

    import pandas as pd
    df = pd.DataFrame(records)

    target_fmt = format_type.lower()
    if target_fmt == "parquet":
        target = Path(out_path) if out_path else Path(f"data/analytics/parquet/{cfg.topic.id}.parquet")
        target.parent.mkdir(parents=True, exist_ok=True)
        df.to_parquet(target, index=False)
        return {"status": "success", "count": len(df), "target": str(target), "format": "parquet"}

    elif target_fmt == "sqlite":
        target = Path(out_path) if out_path else Path(f"data/analytics/{cfg.topic.id}.db")
        target.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        with sqlite3.connect(target) as conn:
            df.to_sql("articles", conn, if_exists="replace", index=False)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_art_portal ON articles(portal);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_art_topic ON articles(topic);")
        return {"status": "success", "count": len(df), "target": str(target), "format": "sqlite"}

    elif target_fmt == "duckdb":
        try:
            import duckdb
        except ImportError:
            return {"status": "error", "message": "duckdb library not installed"}
        target = Path(out_path) if out_path else Path(f"data/analytics/{cfg.topic.id}.duckdb")
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(target))
        conn.execute("CREATE OR REPLACE TABLE articles AS SELECT * FROM df;")
        conn.close()
        return {"status": "success", "count": len(df), "target": str(target), "format": "duckdb"}

    return {"status": "error", "message": f"Unsupported format: {format_type}"}


def generate_topic_dapt_corpus(
    topic_name: str,
    min_words: int = 50,
    out_path: Optional[str] = None
) -> Dict[str, Any]:
    """Compiles clean, normalized text corpus for NusaBERT/IndoBERT domain pretraining."""
    cfg = load_topic_config(topic_name)
    input_file = cfg.get_output_path()
    target_file = Path(out_path) if out_path else cfg.get_dapt_output_path()
    target_file.parent.mkdir(parents=True, exist_ok=True)

    if not input_file.exists():
        return {"status": "error", "message": f"Input file not found: {input_file}"}

    written = 0
    total_words = 0
    seen_hashes = set()

    with open(input_file, "r", encoding="utf-8", errors="replace") as in_f, \
         open(target_file, "w", encoding="utf-8") as out_f:
        for line in in_f:
            if not line.strip():
                continue
            try:
                doc = json.loads(line)
                content = clean_news_text(doc.get("content", ""))
                words = content.split()
                if len(words) < min_words:
                    continue

                # Hash deduplication
                h = hash(content)
                if h in seen_hashes:
                    continue
                seen_hashes.add(h)

                out_f.write(content + "\n\n")
                written += 1
                total_words += len(words)
            except Exception:
                continue

    return {
        "status": "success",
        "documents": written,
        "total_words": total_words,
        "target": str(target_file),
    }


def list_available_topics() -> List[Dict[str, str]]:
    """Discovers and parses all topic YAML definitions in config/topics/."""
    topics_dir = Path("config/topics")
    if not topics_dir.exists():
        return []

    found = []
    for f in sorted(topics_dir.glob("*.yaml")):
        try:
            cfg = load_topic_config(f)
            found.append({
                "id": cfg.topic.id,
                "name": cfg.topic.name,
                "description": cfg.topic.description or "",
                "path": str(f),
            })
        except Exception:
            found.append({
                "id": f.stem,
                "name": f.stem,
                "description": "(Error parsing configuration)",
                "path": str(f),
            })
    return found


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="National News Intelligence Harvester (NNIH)")
    subparsers = parser.add_subparsers(dest="action", help="Topic commands")

    # init action
    init_parser = subparsers.add_parser("init", help="Generate starter topic configuration template")
    init_parser.add_argument("topic", help="Topic ID slug (e.g. karhutla, ikn, pemilu)")
    init_parser.add_argument("--output", "-o", default=None, help="Target YAML file path")

    # validate action
    val_parser = subparsers.add_parser("validate", help="Validate topic configuration syntax and surface")
    val_parser.add_argument("topic", help="Topic ID or YAML file path")

    # list action
    subparsers.add_parser("list", help="List all configured intelligence topics")

    # harvest action
    harv_parser = subparsers.add_parser("harvest", help="Run discovery and extraction for topic")
    harv_parser.add_argument("--topic", "-t", required=True, help="Topic ID (e.g. karhutla)")
    harv_parser.add_argument("--days", "-d", type=int, default=365, help="Past days to crawl (default: 365)")
    harv_parser.add_argument("--workers", "-w", type=int, default=16, help="Worker threads (default: 16)")
    harv_parser.add_argument("--limit", "-l", type=int, default=100000, help="Article harvest limit")
    harv_parser.add_argument("--resume", "-r", action="store_true", help="Resume pending frontier queue")
    harv_parser.add_argument("--stats", action="store_true", help="Display topic frontier stats")
    harv_parser.add_argument("--output", "-o", default=None, help="Output JSONL file path")

    # export action
    exp_parser = subparsers.add_parser("export", help="Export harvested JSONL to Parquet/SQLite/DuckDB")
    exp_parser.add_argument("--topic", "-t", required=True, help="Topic ID")
    exp_parser.add_argument("--format", "-f", default="parquet", choices=["parquet", "sqlite", "duckdb"])
    exp_parser.add_argument("--output", "-o", default=None, help="Target export path")

    # dapt action
    dapt_parser = subparsers.add_parser("dapt", help="Compile text corpus for NusaBERT DAPT MLM")
    dapt_parser.add_argument("--topic", "-t", required=True, help="Topic ID")
    dapt_parser.add_argument("--min-words", "-w", type=int, default=50, help="Minimum word count")
    dapt_parser.add_argument("--output", "-o", default=None, help="Target .txt corpus file")

    args = parser.parse_args()

    if args.action == "init":
        path = generate_topic_template(args.topic, output_path=args.output)
        print(f"Created topic template: {path}")

    elif args.action == "validate":
        rep = validate_topic(args.topic)
        print(f"\n=== Topic Diagnostic: {rep['name']} [{rep['id']}] ===")
        print(f"Description:     {rep['description']}")
        print(f"Total Keywords:  {rep['total_keywords']}")
        print(f"Sample Keywords: {', '.join(rep['sample_keywords'])}")
        print(f"Target Portals:  {rep['total_resolved_portals']} portals resolved")
        for p in rep["portals"][:8]:
            print(f"  * {p['name']} ({p['domain']}) -> {p['adapter']}")
        if len(rep["portals"]) > 8:
            print(f"  ... and {len(rep['portals']) - 8} more portals")
        print(f"Default Output:  {rep['output_path']}")
        print("==================================================")

    elif args.action == "list":
        t_list = list_available_topics()
        print(f"\n=== Configured Intelligence Topics ({len(t_list)}) ===")
        for t in t_list:
            print(f"  * [{t['id']}] {t['name']}")
            if t['description']:
                print(f"      {t['description']}")
        print("==================================================")

    elif args.action == "harvest":
        harvest_topic(
            topic_name=args.topic,
            days=args.days,
            workers=args.workers,
            limit=args.limit,
            resume=args.resume,
            stats_only=args.stats,
            output_override=args.output,
        )

    elif args.action == "export":
        res = export_topic_dataset(args.topic, format_type=args.format, out_path=args.output)
        print(json.dumps(res, indent=2))

    elif args.action == "dapt":
        res = generate_topic_dapt_corpus(args.topic, min_words=args.min_words, out_path=args.output)
        print(json.dumps(res, indent=2))

    else:
        parser.print_help()
