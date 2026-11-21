"""
Live extraction probe: fetch recent URLs for one domain and show what the
pipeline actually pulls out of them.

Used to verify the per-domain extraction rules and the WordPress REST path. Not a
production tool; it makes real network requests unless it is replaying a capture.

    python scripts/probe_extraction.py --domain detik.com --limit 10
    python scripts/probe_extraction.py --url https://... --url https://...
    python scripts/probe_extraction.py --domain detik.com --limit 30 --capture-dir _replay
    python scripts/probe_extraction.py --domain detik.com --replay-dir _replay
    python scripts/probe_extraction.py --report --capture-dir _replay
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backends.curl_impersonate import CurlImpersonateBackend
from backends.wordpress_rest import post_body_by_url
from channels.portal_news import PortalNewsChannel, extract_clean_article_text
from processors.content_policy import MIN_BODY_CHARS
from processors.fetch_pipeline import fetch_article
from processors.portal_catalog import wp_api_base_for

DEFAULT_RAW_DIR = Path("data/raw")


class ReplayMiss(LookupError):
    """Raised when a strict store has no capture for a URL."""


class HtmlStore:
    """Captured HTML keyed by URL, with a JSON sidecar beside each page.

    Layout: <root>/<domain>/<sha1(url)>.html plus .json. The sidecar is what
    makes a replay honest: it records the status and backend that produced the
    HTML, so a replay cannot report a 200 the capture never saw.

    The mode lives on the store, not on the caller. A capturing store never
    serves a page back — the proxy rung deliberately re-fetches the same URL,
    and reading the 403 it just wrote would make the second attempt a copy of
    the first. A replaying store never writes, and raises ReplayMiss instead of
    falling back to the network, which is the only way "replay" means anything.
    """

    def __init__(self, root: Path, replay: bool = False):
        self.root = Path(root)
        self.replay = replay

    def _paths(self, url: str) -> Tuple[Path, Path]:
        domain = urlparse(url).netloc.lower().replace("www.", "") or "_unknown"
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        folder = self.root / domain
        return folder / f"{digest}.html", folder / f"{digest}.json"

    def read(self, url: str) -> Optional[Tuple[int, str]]:
        """Returns (status, html) while replaying; None while capturing."""
        if not self.replay:
            return None
        page, meta = self._paths(url)
        if not page.exists() or not meta.exists():
            raise ReplayMiss(url)
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            text = page.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            raise ReplayMiss(url)
        return int(data.get("status") or 0), text

    def write(self, url: str, status: int, html: Optional[str], backend: str = "") -> None:
        """Stores one response and its sidecar; a no-op while replaying."""
        if self.replay:
            return
        page, meta = self._paths(url)
        page.parent.mkdir(parents=True, exist_ok=True)
        text = html or ""
        raw = text.encode("utf-8")
        page.write_text(text, encoding="utf-8")
        meta.write_text(json.dumps({
            "url": url,
            "status": int(status),
            "backend": backend,
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }, indent=1), encoding="utf-8")

    def report(self) -> Dict[str, Dict[str, Any]]:
        """Per-domain page count, bytes and status mix, read from the sidecars alone."""
        per_domain: Dict[str, Dict[str, Any]] = {}
        for meta in sorted(self.root.glob("*/*.json")):
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            entry = per_domain.setdefault(meta.parent.name, {"pages": 0, "bytes": 0, "statuses": {}})
            entry["pages"] += 1
            entry["bytes"] += int(data.get("bytes") or 0)
            status = str(data.get("status"))
            entry["statuses"][status] = entry["statuses"].get(status, 0) + 1
        return per_domain


def _print_store_report(store: HtmlStore) -> None:
    report = store.report()
    total = sum(entry["pages"] for entry in report.values())
    print(f"store={store.root} | domains={len(report)} | pages={total}")
    for domain, entry in sorted(report.items(), key=lambda kv: -kv[1]["pages"]):
        statuses = " ".join(f"status_{k}={v}" for k, v in sorted(entry["statuses"].items()))
        print(f"domain={domain} | pages={entry['pages']} | bytes={entry['bytes']} | {statuses}")


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


def _run_ladder(urls: List[str], store: HtmlStore) -> int:
    """Runs the real extraction ladder with a store attached, one URL at a time."""

    async def _run() -> int:
        curl = CurlImpersonateBackend()
        channel = PortalNewsChannel()
        session = curl.create_async_session()
        try:
            for url in urls:
                result = await fetch_article(
                    session,
                    curl,
                    channel,
                    url,
                    MIN_BODY_CHARS,
                    fallback_min_content_length=1,
                    html_source=store,
                )
                print(
                    f"url={url} | backend={result.backend} | label={result.label}"
                    f" | quality={result.quality} | chars={result.content_chars}"
                    f" | reason={result.reason}"
                )
        finally:
            await session.close()
        return 0

    return asyncio.run(_run())


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe live extraction for one domain.")
    parser.add_argument("--domain", default=None, help="portal domain, e.g. detik.com")
    parser.add_argument("--url", action="append", default=[], help="explicit URL (repeatable)")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--capture-dir", default=None, help="record every response under this directory")
    parser.add_argument("--replay-dir", default=None, help="replay a capture and refuse the network")
    parser.add_argument("--report", action="store_true", help="print the store report and exit")
    args = parser.parse_args()

    if args.report:
        root = args.replay_dir or args.capture_dir
        if not root:
            parser.error("--report needs --capture-dir or --replay-dir")
        _print_store_report(HtmlStore(Path(root)))
        return 0

    if not args.domain and not args.url:
        parser.error("pass --domain or at least one --url")

    urls = list(args.url)
    if args.domain:
        urls.extend(_recent_urls(args.domain, args.limit, Path(args.raw_dir)))
    if not urls:
        print(f"no harvested URLs found for {args.domain}")
        return 1

    if args.capture_dir or args.replay_dir:
        store = HtmlStore(Path(args.replay_dir or args.capture_dir), replay=bool(args.replay_dir))
        return _run_ladder(urls, store)

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
