"""
Re-harvests the records that came back as fragments.

Reads a JSONL harvest file, picks the records tagged quality="low" (or written
before tagging existed and shorter than the threshold), fetches those URLs again
through the normal ladder, and writes the improved records to a side file.

    python scripts/reharvest_low_quality.py --path data/raw/news/football.jsonl --limit 20
    python scripts/reharvest_low_quality.py --path data/raw/news --domain detik.com --merge

Nothing is rewritten until --merge is passed, and --merge keeps a .bak first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backends.curl_impersonate import CurlImpersonateBackend
from channels.portal_news import PortalNewsChannel
from processors.cleaner import deduplicate_jsonl_file
from processors.fetch_pipeline import fetch_article
from processors.portal_catalog import wp_api_base_for
from processors.record_writer import MIN_QUALITY_CHARS, quality_of

SIDE_SUFFIX = ".reharvest.jsonl"


def _iter_jsonl(path: Path):
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _needs_reharvest(record: Dict[str, Any], min_chars: int) -> bool:
    """True for fragments: tagged low, or untagged and shorter than the threshold."""
    content = record.get("content") or ""
    if record.get("quality") == "low":
        return True
    if record.get("quality") is None:
        return len(content) < min_chars
    return False


def select_targets(
    paths: List[Path],
    min_chars: int = MIN_QUALITY_CHARS,
    domain: Optional[str] = None,
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Collects the records worth re-fetching, deduplicated by URL."""
    targets: List[Dict[str, Any]] = []
    seen = set()
    for path in paths:
        for record in _iter_jsonl(path):
            url = record.get("url")
            if not url or url in seen:
                continue
            if domain and domain.lower() not in urlparse(url).netloc.lower():
                continue
            if not _needs_reharvest(record, min_chars):
                continue
            seen.add(url)
            record["_source_file"] = str(path)
            targets.append(record)
            if limit and len(targets) >= limit:
                return targets
    return targets


def _improved_record(original: Dict[str, Any], result) -> Dict[str, Any]:
    """Keeps the original record shape and replaces only what the fetch produced."""
    record = dict(original)
    record.pop("_source_file", None)
    record["content"] = result.content
    record["content_chars"] = result.content_chars
    record["quality"] = result.quality
    record["backend"] = result.backend
    if result.title and not record.get("title"):
        record["title"] = result.title
    if result.published_date and not record.get("published_date") and not record.get("publish_date"):
        record["published_date"] = result.published_date
    record["reharvested"] = True
    return record


async def reharvest(
    targets: List[Dict[str, Any]],
    concurrency: int = 8,
    min_content_length: int = 120,
    min_chars: int = MIN_QUALITY_CHARS,
) -> List[Dict[str, Any]]:
    """Fetches every target again and returns the records that actually improved."""
    curl_backend = CurlImpersonateBackend()
    channel = PortalNewsChannel()
    session = curl_backend.create_async_session()
    semaphore = asyncio.Semaphore(concurrency)
    improved: List[Dict[str, Any]] = []

    async def _one(original: Dict[str, Any]) -> None:
        url = original["url"]
        async with semaphore:
            try:
                result = await fetch_article(
                    session,
                    curl_backend,
                    channel,
                    url,
                    min_content_length=min_content_length,
                    fallback_min_content_length=1,
                    wp_api_base=wp_api_base_for(urlparse(url).netloc.replace("www.", "")),
                    min_quality_chars=min_chars,
                )
            except Exception as exc:
                print(f"  ! {url}: {exc}", file=sys.stderr)
                return
        if result.content is None:
            return
        if result.content_chars <= len(original.get("content") or ""):
            return
        improved.append(_improved_record(original, result))

    try:
        await asyncio.gather(*(_one(target) for target in targets))
    finally:
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass
    return improved


def write_side_file(records: List[Dict[str, Any]], path: Path) -> Path:
    side = path.with_suffix(path.suffix + SIDE_SUFFIX)
    with open(side, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return side


def merge_into(path: Path, side: Path) -> int:
    """Appends the side file to the target and keeps the longest body per URL."""
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    with open(path, "a", encoding="utf-8") as target, open(side, "r", encoding="utf-8") as source:
        for line in source:
            if line.strip():
                target.write(line if line.endswith("\n") else line + "\n")
    removed = deduplicate_jsonl_file(path, prefer="longest")
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description="Re-harvest low quality article records.")
    parser.add_argument("--path", required=True, help="JSONL file or directory of JSONL files")
    parser.add_argument("--domain", default=None, help="Only URLs whose host contains this")
    parser.add_argument("--limit", type=int, default=None, help="Cap on records to re-fetch")
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--min-chars", type=int, default=MIN_QUALITY_CHARS)
    parser.add_argument("--merge", action="store_true", help="Append results and dedupe in place")
    parser.add_argument("--dry-run", action="store_true", help="Only report the selection")
    args = parser.parse_args()

    target = Path(args.path)
    paths = sorted(target.glob("*.jsonl")) if target.is_dir() else [target]
    if not paths:
        print(f"no JSONL files under {target}", file=sys.stderr)
        return 1

    targets = select_targets(paths, min_chars=args.min_chars, domain=args.domain, limit=args.limit)
    print(f"selected {len(targets)} records below {args.min_chars} chars")
    if args.dry_run or not targets:
        for record in targets[:10]:
            print(f"  {len(record.get('content') or ''):>6}  {record['url']}")
        return 0

    improved = asyncio.run(
        reharvest(targets, concurrency=args.concurrency, min_chars=args.min_chars)
    )
    print(f"improved {len(improved)} of {len(targets)}")

    if improved:
        before = [len(target.get("content") or "") for target in targets]
        after = [record["content_chars"] for record in improved]
        print(f"median chars: {sorted(before)[len(before) // 2]} -> {sorted(after)[len(after) // 2]}")

    side = write_side_file(improved, paths[0])
    print(f"wrote {side}")

    if args.merge and improved:
        removed = merge_into(paths[0], side)
        print(f"merged into {paths[0]} (removed {removed} duplicate rows, backup at {paths[0]}.bak)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
