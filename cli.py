"""
Command Line Interface for Indonesian Sports Scrapper.
Usage:
    python cli.py doctor
    python cli.py search --query "Dewa United" --sport football --limit 5
    python cli.py extract --url "https://www.antaranews.com/..."
"""

import argparse
import json
import sys

from doctor import run_doctor, format_doctor_report
from channels import get_channel, get_all_channels


def main():
    parser = argparse.ArgumentParser(description="Indonesian Sports Multi-Channel Harvester")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # doctor command
    doc_parser = subparsers.add_parser("doctor", help="Run health check on channels & dependencies")
    doc_parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")

    # search command
    search_parser = subparsers.add_parser("search", help="Search sports articles across channels")
    search_parser.add_argument("--query", "-q", required=True, help="Search keyword")
    search_parser.add_argument("--sport", "-s", default="football", choices=["football", "basketball", "badminton", "volleyball"])
    search_parser.add_argument("--limit", "-l", type=int, default=10, help="Max results")
    search_parser.add_argument("--channel", "-c", default="portal_news", help="Channel identifier")

    # extract command
    extract_parser = subparsers.add_parser("extract", help="Extract cleaned content from URL")
    extract_parser.add_argument("--url", "-u", required=True, help="Target article URL")
    extract_parser.add_argument("--channel", "-c", default="portal_news", help="Channel identifier")

    # sync-firefox-twitter command
    subparsers.add_parser("sync-firefox-twitter", help="Auto-extract Twitter session from local Firefox profile")

    # set-twitter-session command
    session_parser = subparsers.add_parser("set-twitter-session", help="Manually save Twitter auth_token and ct0 tokens")
    session_parser.add_argument("--auth-token", required=True, help="Twitter auth_token cookie")
    session_parser.add_argument("--ct0", required=True, help="Twitter ct0 CSRF cookie")

    # harvest command
    harvest_parser = subparsers.add_parser("harvest", help="Run local sports harvest cycle across all channels")
    harvest_parser.add_argument("--limit", "-l", type=int, default=5, help="Items per target/keyword")
    harvest_parser.add_argument("--workers", "-w", type=int, default=5, help="Concurrent worker threads (default: 5)")
    harvest_parser.add_argument("--no-news", action="store_true", help="Skip news portal harvesting")
    harvest_parser.add_argument("--no-youtube", action="store_true", help="Skip YouTube harvesting")
    harvest_parser.add_argument("--no-twitter", action="store_true", help="Skip Twitter/X harvesting")
    harvest_parser.add_argument("--proxy", default=None, help="Direct HTTP proxy endpoint (e.g. http://user:pass@ip:port)")
    harvest_parser.add_argument("--proxies-file", default=None, help="Path to text file containing rotating proxy list")

    # export command
    export_parser = subparsers.add_parser("export", help="Export raw JSONL data into high-performance SQLite, DuckDB, or Parquet datasets")
    export_parser.add_argument("--format", "-f", default="sqlite", choices=["sqlite", "duckdb", "parquet"], help="Analytical output format (default: sqlite)")
    export_parser.add_argument("--output", "-o", default=None, help="Output file/directory path")
    export_parser.add_argument("--raw-dir", default="data/raw", help="Base raw data directory (default: data/raw)")
    export_parser.add_argument("--channels", "-c", nargs="+", choices=["news", "youtube", "twitter"], default=None, help="Channels to export (default: all)")

    # backup command
    backup_parser = subparsers.add_parser("backup", help="Create gzip compressed archive of datasets and sync to cloud storage")
    backup_parser.add_argument("--output-dir", "-o", default="data/backups", help="Destination backup directory (default: data/backups)")
    backup_parser.add_argument("--upload-url", "-u", default=None, help="Presigned S3/R2 PUT URL for automated cloud cold-storage sync")

    # backfill command
    backfill_parser = subparsers.add_parser("backfill", help="Harvest historical football articles via XML sitemaps and archives")
    backfill_parser.add_argument(
        "--portal", "-p",
        choices=[
            "detik", "kompas", "sindonews", "okezone", "suara", "republika", "antara",
            "bolasport", "bolanet", "tribun", "jawapos", "skor",
            "beritajatim", "balipost", "fajar", "bantennews", "niaga_asia", "all"
        ],
        default=None,
        help="Target portal (default: all configured portals)"
    )
    backfill_parser.add_argument("--sitemap", "-s", default=None, help="Direct XML sitemap URL to parse")
    backfill_parser.add_argument("--days", "-d", type=int, default=None, help="Past days to crawl (e.g. 365 for 1 year, 7 for weekly)")
    backfill_parser.add_argument("--start-date", default=None, help="Archive start date (YYYY-MM-DD)")
    backfill_parser.add_argument("--end-date", default=None, help="Archive end date (YYYY-MM-DD)")
    backfill_parser.add_argument("--mode", "-m", choices=["sitemap", "archive", "both"], default="both", help="Harvesting mode (default: both)")
    backfill_parser.add_argument("--resume", "-r", action="store_true", help="Resume pending crawl queue from frontier database")
    backfill_parser.add_argument("--stats", action="store_true", help="Display crawl frontier progress statistics")
    backfill_parser.add_argument("--limit", "-l", type=int, default=100, help="Maximum articles to harvest")
    backfill_parser.add_argument("--workers", "-w", type=int, default=5, help="Concurrent extraction threads (default: 5)")
    backfill_parser.add_argument("--cores", "-c", type=int, default=2, help="CPU cores allocated for parallel process execution (default: 2)")
    backfill_parser.add_argument("--output", "-o", default="data/raw/news/football.jsonl", help="Output JSONL file (default: data/raw/news/football.jsonl)")
    backfill_parser.add_argument("--proxy", default=None, help="Direct HTTP proxy endpoint (e.g. http://user:pass@ip:port)")
    backfill_parser.add_argument("--proxies-file", default=None, help="Path to text file containing rotating proxy list")

    # dapt-corpus command
    dapt_parser = subparsers.add_parser("dapt-corpus", help="Prepare clean text corpus from raw articles for NusaBERT DAPT MLM")
    dapt_parser.add_argument("--input", "-i", default="data/raw/news/football.jsonl", help="Input raw JSONL file")
    dapt_parser.add_argument("--output", "-o", default="data/processed/nusabert_football_dapt.txt", help="Output corpus .txt path")
    dapt_parser.add_argument("--min-words", "-w", type=int, default=50, help="Minimum words per article (default: 50)")
    dapt_parser.add_argument("--format", "-f", choices=["line", "document"], default="line", help="Layout format: 'line' or 'document' (default: line)")

    # topic command (National News Intelligence Harvester)
    topic_parser = subparsers.add_parser("topic", help="National News Intelligence Harvester (NNIH) topic operations")
    topic_subparsers = topic_parser.add_subparsers(dest="topic_action", help="Topic actions")

    # topic init
    t_init = topic_subparsers.add_parser("init", help="Generate starter topic configuration template")
    t_init.add_argument("topic", help="Topic ID slug (e.g. karhutla, ikn, pemilu)")
    t_init.add_argument("--output", "-o", default=None, help="Target YAML file path")

    # topic validate
    t_val = topic_subparsers.add_parser("validate", help="Validate topic configuration syntax and portal surface")
    t_val.add_argument("topic", help="Topic ID or YAML file path")

    # topic list
    topic_subparsers.add_parser("list", help="List all configured intelligence topics")

    # topic harvest
    t_harv = topic_subparsers.add_parser("harvest", help="Run discovery and extraction for topic")
    t_harv.add_argument("--topic", "-t", required=True, help="Topic ID (e.g. karhutla)")
    t_harv.add_argument("--days", "-d", type=int, default=365, help="Past days to crawl (default: 365)")
    t_harv.add_argument("--cores", "-c", type=int, default=2, help="CPU cores allocated (default: 2)")
    t_harv.add_argument("--concurrency", type=int, default=25, help="Concurrent async sockets per core (default: 25)")
    t_harv.add_argument("--workers", "-w", type=int, default=16, help="Worker threads/alias (default: 16)")
    t_harv.add_argument("--limit", "-l", type=int, default=100000, help="Article harvest limit")
    t_harv.add_argument("--resume", "-r", action="store_true", help="Resume pending frontier queue")
    t_harv.add_argument("--stats", action="store_true", help="Display topic frontier stats")
    t_harv.add_argument("--output", "-o", default=None, help="Output JSONL file path")

    # topic export
    t_exp = topic_subparsers.add_parser("export", help="Export harvested JSONL to Parquet/SQLite/DuckDB")
    t_exp.add_argument("--topic", "-t", required=True, help="Topic ID")
    t_exp.add_argument("--format", "-f", default="parquet", choices=["parquet", "sqlite", "duckdb"])
    t_exp.add_argument("--output", "-o", default=None, help="Target export path")

    # topic dapt
    t_dapt = topic_subparsers.add_parser("dapt", help="Compile text corpus for NusaBERT DAPT MLM")
    t_dapt.add_argument("--topic", "-t", required=True, help="Topic ID")
    t_dapt.add_argument("--min-words", "-w", type=int, default=50, help="Minimum word count")
    t_dapt.add_argument("--output", "-o", default=None, help="Target .txt corpus file")

    args = parser.parse_args()

    if args.command == "doctor":
        res = run_doctor()
        if args.json:
            print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            print(format_doctor_report(res))

    elif args.command == "search":
        ch = get_channel(args.channel)
        if not ch:
            print(f"[!] Error: Channel '{args.channel}' not found.")
            sys.exit(1)

        results = ch.search(query=args.query, sport=args.sport, limit=args.limit)
        print(f"[*] Found {len(results)} articles for '{args.query}' ({args.sport}):\n")
        for i, item in enumerate(results, 1):
            print(f"{i}. [{item.get('source')}] {item.get('title')}")
            print(f"   Date: {item.get('published_date')}")
            print(f"   URL:  {item.get('url')}\n")

    elif args.command == "extract":
        ch = get_channel(args.channel)
        if not ch:
            print(f"[!] Error: Channel '{args.channel}' not found.")
            sys.exit(1)

        result = ch.extract(args.url)
        if result.get("status") == "success":
            print(f"[*] Successfully extracted via {result.get('backend')}:\n")
            print(result.get("content")[:1000])
            if len(result.get("content", "")) > 1000:
                print("\n... [truncated] ...")
        else:
            print(f"[!] Extraction failed: {result.get('error', 'Unknown error')}")

    elif args.command == "sync-firefox-twitter":
        from backends.firefox_cookies import extract_firefox_twitter_tokens, save_twitter_session
        print("[*] Scanning Firefox profiles for active Twitter session...")
        tokens = extract_firefox_twitter_tokens()
        if tokens:
            save_twitter_session(tokens)
            print(f"[+] Extracted active session from Firefox profile '{tokens.get('profile')}'.")
            print("[+] Saved to config/twitter_session.json. GraphQL backend is now active!")
        else:
            print("[!] No active Twitter session found in Firefox cookies.")
            print("    Make sure you have logged into x.com in Firefox.")

    elif args.command == "set-twitter-session":
        from backends.firefox_cookies import save_twitter_session
        tokens = {
            "auth_token": args.auth_token,
            "ct0": args.ct0,
            "profile": "manual_input"
        }
        save_twitter_session(tokens)
        print("[+] Manually saved Twitter session to config/twitter_session.json.")

    elif args.command == "harvest":
        from processors.http_client import default_http_client
        if getattr(args, "proxy", None):
            default_http_client.set_single_proxy(args.proxy)
            print(f"[*] Configured proxy: {args.proxy}")
        if getattr(args, "proxies_file", None):
            loaded_cnt = default_http_client.load_proxies_from_file(args.proxies_file)
            print(f"[*] Loaded {loaded_cnt} rotating proxies from '{args.proxies_file}'")
        else:
            from processors.http_client import DEFAULT_PROXIES_FILE
            if DEFAULT_PROXIES_FILE.exists():
                loaded_cnt = default_http_client.load_proxies_from_file(str(DEFAULT_PROXIES_FILE))
                if loaded_cnt:
                    print(f"[*] Auto-loaded {loaded_cnt} rotating proxies from '{DEFAULT_PROXIES_FILE}'")

        from scripts.topic_harvester import harvest_topic
        print("[*] Notice: Legacy 'harvest' redirected to NNIH 2-Core Async Engine for topic 'sports'.")
        harvest_topic(
            topic_name="sports",
            limit=args.limit * 50,
            cores=2,
            concurrency=25,
        )

    elif args.command == "export":
        from processors.exporter import run_export
        print(f"[*] Exporting raw JSONL to analytical format '{args.format}'...")
        res = run_export(
            format_type=args.format,
            raw_data_dir=args.raw_dir,
            output_path=args.output,
            channels=args.channels
        )
        if res.get("status") == "success":
            print(f"[+] Export succeeded! Target: {res.get('target')}")
            counts = res.get("counts", {})
            for table, cnt in counts.items():
                print(f"    - {table}: {cnt:,} records")
        else:
            print(f"[!] Export failed: {res.get('message')}")
            sys.exit(1)

    elif args.command == "backup":
        from processors.backup import run_backup
        print("[*] Creating compressed archive of raw datasets and analytics...")
        res = run_backup(output_dir=args.output_dir, upload_url=args.upload_url)
        if res.get("status") == "success":
            print(f"[+] Backup completed successfully!")
            print(f"    - Archive: {res.get('archive_path')}")
            print(f"    - Size:    {res.get('size_mb')} MB ({res.get('size_bytes'):,} bytes)")
            if res.get("cloud_uploaded"):
                print("    - Cloud Sync: Presigned PUT upload succeeded.")
        else:
            print(f"[!] Backup failed: {res.get('message')}")
            sys.exit(1)

    elif args.command == "backfill":
        from processors.http_client import default_http_client
        if getattr(args, "proxy", None):
            default_http_client.set_single_proxy(args.proxy)
            print(f"[*] Configured proxy: {args.proxy}")
        if getattr(args, "proxies_file", None):
            loaded_cnt = default_http_client.load_proxies_from_file(args.proxies_file)
            print(f"[*] Loaded {loaded_cnt} rotating proxies from '{args.proxies_file}'")
        else:
            from processors.http_client import DEFAULT_PROXIES_FILE
            if DEFAULT_PROXIES_FILE.exists():
                loaded_cnt = default_http_client.load_proxies_from_file(str(DEFAULT_PROXIES_FILE))
                if loaded_cnt:
                    print(f"[*] Auto-loaded {loaded_cnt} rotating proxies from '{DEFAULT_PROXIES_FILE}'")

        if args.stats:
            from processors.crawl_frontier import CrawlFrontier
            frontier = CrawlFrontier()
            summary = frontier.get_frontier_summary()
            print("\n=== Indonesian Sports Scraper - Crawl Frontier Stats ===")
            totals = summary.get("totals", {})
            print(f"Total Discovered: {totals.get('total', 0):,}")
            print(f"  - Completed:    {totals.get('completed', 0):,}")
            print(f"  - Pending:      {totals.get('pending', 0):,}")
            print(f"  - In Progress:  {totals.get('in_progress', 0):,}")
            print(f"  - Failed:       {totals.get('failed', 0):,}")
            print("\nBreakdown by Portal:")
            for p, p_data in summary.get("portals", {}).items():
                print(f"  * {p}: {p_data.get('total', 0):,} total ({p_data.get('completed', 0):,} done, {p_data.get('pending', 0):,} pending, {p_data.get('failed', 0):,} failed)")
            print("========================================================\n")
        else:
            from scripts.sitemap_backfill import run_sitemap_backfill
            mode_desc = f"mode: {args.mode}"
            if args.days:
                mode_desc += f", days: {args.days}"
            elif args.start_date and args.end_date:
                mode_desc += f", range: {args.start_date} to {args.end_date}"
            if args.resume:
                mode_desc += ", resume: active"

            print(f"[*] Starting historical backfill ({mode_desc}, limit: {args.limit} articles)...")
            res = run_sitemap_backfill(
                portal=args.portal,
                sitemap_url=args.sitemap,
                start_date=args.start_date,
                end_date=args.end_date,
                days=args.days,
                mode=args.mode,
                resume=args.resume,
                output_file=args.output,
                max_articles=args.limit,
                max_workers=args.workers,
                cores=args.cores,
            )
            if res.get("status") == "success":
                print(f"[+] Backfill batch completed!")
                print(f"    - Articles Collected (This Batch): {res.get('collected', 0):,}")
                print(f"    - New Candidates Discovered:      {res.get('new_candidates_added', 0):,}")
                print(f"    - Destination:                     {res.get('output_file')}")
                totals = res.get("frontier_summary", {}).get("totals", {})
                print(f"    - Frontier Total Completed:        {totals.get('completed', 0):,}")
                print(f"    - Frontier Pending Remaining:      {totals.get('pending', 0):,}")
            else:
                print(f"[!] Backfill failed: {res.get('message')}")
                sys.exit(1)

    elif args.command == "dapt-corpus":
        from scripts.topic_harvester import generate_topic_dapt_corpus
        print("[*] Notice: Legacy 'dapt-corpus' redirected to NNIH DAPT engine for topic 'sports'.")
        res = generate_topic_dapt_corpus(
            topic_name="sports",
            min_words=args.min_words,
            out_path=args.output
        )
        print(json.dumps(res, indent=2))

    elif args.command == "topic":
        from scripts.topic_harvester import (
            generate_topic_template,
            validate_topic,
            list_available_topics,
            harvest_topic,
            export_topic_dataset,
            generate_topic_dapt_corpus,
        )
        if args.topic_action == "init":
            path = generate_topic_template(args.topic, output_path=args.output)
            print(f"[+] Created starter topic template: {path}")

        elif args.topic_action == "validate":
            try:
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
            except Exception as e:
                print(f"[!] Validation failed: {e}")
                sys.exit(1)

        elif args.topic_action == "list":
            t_list = list_available_topics()
            print(f"\n=== Configured Intelligence Topics ({len(t_list)}) ===")
            for t in t_list:
                print(f"  * [{t['id']}] {t['name']}")
                if t['description']:
                    print(f"      {t['description']}")
            print("==================================================")

        elif args.topic_action == "harvest":
            harvest_topic(
                topic_name=args.topic,
                days=args.days,
                cores=args.cores,
                concurrency=args.concurrency,
                workers=args.workers,
                limit=args.limit,
                resume=args.resume,
                stats_only=args.stats,
                output_override=args.output,
            )

        elif args.topic_action == "export":
            res = export_topic_dataset(args.topic, format_type=args.format, out_path=args.output)
            print(json.dumps(res, indent=2))

        elif args.topic_action == "dapt":
            res = generate_topic_dapt_corpus(args.topic, min_words=args.min_words, out_path=args.output)
            print(json.dumps(res, indent=2))

        else:
            topic_parser.print_help()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
