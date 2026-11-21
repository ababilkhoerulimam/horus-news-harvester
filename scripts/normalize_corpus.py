"""
Rewrites the saved corpus through the current cleaning rules.

Five passes per record, in this order: html.unescape, U+00A0 to space,
normalize_article, clean_news_text, then shape_decision for quality. The order
matters. Unescape runs first so entities become the characters the rules look
for. The nbsp replacement runs before normalize_article because U+00A0 is a
space to neither cleaner, and a caption ending in one would never match a
credit tail.

Only content, quality and content_chars change. Every other key is copied
through untouched, including title, which normalize_title deliberately does not
touch yet. Records written before quality existed gain that key and
content_chars, so the key set can grow by two; nothing is dropped or renamed.
Measured on the real corpus, 225.631 of 225.632 records were missing both.

    python scripts/normalize_corpus.py --path data/raw/news --dry-run --report
    python scripts/normalize_corpus.py --path /tmp/copy.jsonl --apply

--apply writes a .bak beside the file, streams the new records into a temp file
in the same directory, then replaces the original. Reading and writing an 851 MB
file at once is how a corpus gets lost.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from processors.cleaner import clean_news_text, normalize_article  # noqa: E402
from processors.content_policy import shape_decision  # noqa: E402

DEFAULT_NEWS_DIR = Path("data/raw/news")

_ENTITY = re.compile(r"&(?:[a-zA-Z][a-zA-Z0-9]{1,31}|#\d{1,7}|#x[0-9a-fA-F]{1,6});")
_NBSP = "\xa0"
_FLAG_KEYS = ("changed", "article_only", "relabelled", "emptied", "entity")

# Two cleaners that are each idempotent are not idempotent together:
# clean_news_text removes a "Baca juga" line, which can expose a dateline that
# normalize_article has already walked past. Measured on 5.000 detik records,
# one needed a second pass and none needed a third.
_CLEAN_PASSES = 3


def normalize_content(content: str) -> str:
    """Unescapes once, then repeats the two cleaners until they stop moving.

    Unescape runs once on purpose. Running it per pass would decode
    double-encoded text twice, and "&amp;lt;" in an article is a literal, not an
    escape. This matches the rule-based extractor, which unescapes once
    (processors/extraction_rules.py). The consequence is that a page stored as
    "&amp;nbsp;" needs a second run to lose the layer, which is why --apply is
    idempotent on ordinary records and not on the 255 double-encoded ones.
    """
    if not content:
        return content
    text = html_lib.unescape(content).replace(_NBSP, " ")
    for _ in range(_CLEAN_PASSES):
        before = text
        text = clean_news_text(normalize_article(text))
        if text == before:
            break
    return text


def normalize_record(record: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, bool]]:
    """Returns the rewritten record and what moved: changed, relabelled, emptied."""
    original = record.get("content") or ""
    content = normalize_content(original)
    _, quality, _ = shape_decision(content, record.get("url") or "")

    updated = dict(record)
    updated["content"] = content
    updated["quality"] = quality
    updated["content_chars"] = len(content)

    flags = {
        "changed": content != original,
        "relabelled": record.get("quality") != quality,
        "emptied": bool(original.strip()) and not content.strip(),
        "entity": bool(_ENTITY.search(original)),
    }
    return updated, flags


def iter_records(paths: Iterable[Path], limit: Optional[int] = None) -> Iterator[Dict[str, Any]]:
    """Yields parsed records from JSONL files, skipping blank and broken lines."""
    for path in paths:
        seen = 0
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if limit is not None and seen >= limit:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    record = json.loads(stripped)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                seen += 1
                yield record


def write_with_backup(path: Path, records: Iterable[Dict[str, Any]]) -> Path:
    """Writes records to a temp file beside the original, keeps a .bak, then replaces."""
    backup = path.with_suffix(path.suffix + ".bak")
    shutil.copy2(path, backup)
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        temp_path.replace(path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return backup


def domain_report(rows: Iterable[Tuple[str, Dict[str, bool], str]]) -> Dict[str, Any]:
    """Aggregates (domain, flags, label) rows into per-domain counters and totals."""
    domains: Dict[str, Dict[str, Any]] = {}
    totals = Counter()
    for domain, flags, label in rows:
        entry = domains.setdefault(
            domain,
            {"records": 0, "labels": Counter(), **{key: 0 for key in _FLAG_KEYS}},
        )
        entry["records"] += 1
        entry["labels"][label] += 1
        for key in _FLAG_KEYS:
            if flags.get(key):
                entry[key] += 1
        totals["records"] += 1
        for key in _FLAG_KEYS:
            totals[key] += int(flags.get(key, False))
    return {"domains": domains, "totals": dict(totals)}


def _domain_of(record: Dict[str, Any]) -> str:
    return urlparse(record.get("url") or "").netloc.lower().replace("www.", "") or "(none)"


def _rows(path: Path, limit: Optional[int]) -> Iterator[Tuple[str, Dict[str, bool], str]]:
    for record in iter_records([path], limit):
        _, flags = normalize_record(record)
        # The Phase 2 gate counted a record as changed when normalize_article
        # alone moved its length. The five-pass pipeline changes far more
        # records because clean_news_text collapses whitespace in nearly all of
        # them, so the two numbers are reported separately instead of compared.
        original = (record.get("content") or "").strip()
        flags["article_only"] = len(normalize_article(original)) != len(original)
        _, quality, _ = shape_decision(record.get("content") or "", record.get("url") or "")
        yield _domain_of(record), flags, quality


def _print_report(report: Dict[str, Any]) -> None:
    totals = report["totals"]
    print(
        f"records={totals.get('records', 0)} | changed={totals.get('changed', 0)}"
        f" | changed_article_only={totals.get('article_only', 0)}"
        f" | relabelled={totals.get('relabelled', 0)}"
        f" | emptied={totals.get('emptied', 0)}"
        f" | entities={totals.get('entity', 0)}"
    )
    labels: Counter = Counter()
    for entry in report["domains"].values():
        labels.update(entry["labels"])
    total = max(totals.get("records", 0), 1)
    for label, count in labels.most_common():
        print(f"label={label} | records={count} | share={count / total:.4f}")
    for domain, entry in sorted(report["domains"].items(), key=lambda kv: -kv[1]["records"])[:15]:
        print(
            f"domain={domain} | records={entry['records']} | changed={entry['changed']}"
            f" | relabelled={entry['relabelled']} | emptied={entry['emptied']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize the saved news corpus in place.")
    parser.add_argument("--path", default=str(DEFAULT_NEWS_DIR), help="JSONL file or directory")
    parser.add_argument("--dry-run", action="store_true", help="report only; this is the default")
    parser.add_argument("--report", action="store_true", help="print per-domain and per-label counts")
    parser.add_argument("--apply", action="store_true", help="rewrite the files, keeping a .bak")
    parser.add_argument("--limit", type=int, default=None, help="cap records per file")
    args = parser.parse_args()

    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    target = Path(args.path)
    paths = sorted(target.glob("*.jsonl")) if target.is_dir() else [target]
    if not paths:
        print(f"no JSONL files under {target}")
        return 1

    if args.apply:
        for path in paths:
            size = path.stat().st_size
            free = shutil.disk_usage(path.parent).free
            if free < 2 * size:
                print(f"refused path={path} | need={2 * size} | free={free}")
                return 1

    rows: List[Tuple[str, Dict[str, bool], str]] = []
    for path in paths:
        if args.apply:

            def _rewritten(p: Path = path) -> Iterator[Dict[str, Any]]:
                for record in iter_records([p], args.limit):
                    updated, _ = normalize_record(record)
                    yield updated

            backup = write_with_backup(path, _rewritten())
            print(f"wrote={path} | backup={backup}")
        rows.extend(_rows(path, args.limit))

    report = domain_report(rows)
    if args.report or not args.apply:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
