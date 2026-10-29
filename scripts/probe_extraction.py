"""
Live extraction probe: fetch recent URLs for one domain and show what the
pipeline actually pulls out of them.

Used to verify the per-domain extraction rules and the WordPress REST path. Not a
production tool; it makes real network requests.

    python scripts/probe_extraction.py --domain detik.com --limit 10
    python scripts/probe_extraction.py --url https://... --url https://...
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from pathlib import Path
from typing import Deque, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backends.curl_impersonate import CurlImpersonateBackend
from backends.wordpress_rest import post_body_by_url
from channels.portal_news import extract_clean_article_text
from processors.portal_catalog import wp_api_base_for

DEFAULT_RAW_DIR = Path("data/raw")


def _recent_urls(domain: str, limit: int, raw_dir: Path) -> List[str]:
    """Returns the last N harvested URLs for a domain, streaming every news file once."""
    urls: Deque[str] = deque(maxlen=limit)
    for path in sorted((raw_dir / "news").glob("*.jsonl")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if domain not in line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    url = record.get("url") or ""
                    if domain in url:
                        urls.append(url)
        except OSError:
            continue
    return list(urls)


def _rule_chars(html: str, domain: str) -> Optional[int]:
    """Character count under the per-domain rule, when that module exists."""
    try:
        from processors.extraction_rules import apply_rule, rules_for
    except ImportError:
        return None
    rule = rules_for(domain)
    if rule is None:
        return None
    extracted = apply_rule(html, rule)
    return len(extracted) if extracted else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe live extraction for one domain.")
    parser.add_argument("--domain", default=None, help="portal domain, e.g. detik.com")
    parser.add_argument("--url", action="append", default=[], help="explicit URL (repeatable)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    args = parser.parse_args()

    if not args.domain and not args.url:
        parser.error("pass --domain or at least one --url")

    urls = list(args.url)
    if args.domain:
        urls.extend(_recent_urls(args.domain, args.limit, Path(args.raw_dir)))
    if not urls:
        print(f"no harvested URLs found for {args.domain}")
        return 1

    backend = CurlImpersonateBackend()
    print(f"{'status':>6}{'ms':>7}{'html KB':>9}{'now':>8}{'rule':>8}{'wp':>8}  url")
    print("-" * 108)

    for url in urls:
        started = time.perf_counter()
        status, html = backend.fetch(url)
        elapsed = (time.perf_counter() - started) * 1000
        chars_now = len(extract_clean_article_text(html)) if status == 200 and html else 0
        chars_rule = _rule_chars(html, args.domain) if status == 200 and html and args.domain else None

        wp_base = wp_api_base_for(args.domain) if args.domain else None
        wp_body = post_body_by_url(wp_base, url, backend=backend) if wp_base else None
        wp_cell = "-" if not wp_base else str(len(wp_body["content"]) if wp_body else 0)

        rule_cell = "-" if chars_rule is None else str(chars_rule)
        print(
            f"{status:>6}{elapsed:>7.0f}{len(html or '') / 1024:>9.0f}"
            f"{chars_now:>8}{rule_cell:>8}{wp_cell:>8}  {url[:60]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
