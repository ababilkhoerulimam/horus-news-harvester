"""
Harvest yield report: which domains and videos actually produce usable content.

Reads the JSONL output offline and reports, per portal domain, how often the fast
path was used and how much text came back. This is the measuring stick for every
extraction change; without it a fix cannot be proven.

    python scripts/report_yield.py
    python scripts/report_yield.py --channel news --top 15
    python scripts/report_yield.py --json > yield.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_RAW_DIR = Path("data/raw")

# Backends that mean "the first attempt worked". Anything else went down the
# fallback ladder, which costs seconds per article instead of milliseconds.
FAST_BACKENDS = frozenset({"curl_impersonate", "wordpress_rest"})


def _iter_jsonl(paths: Iterable[Path]) -> Iterable[Dict[str, Any]]:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(record, dict):
                        yield record
        except OSError:
            continue


def _percentile(values: List[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def news_report(raw_dir: Path) -> Dict[str, Any]:
    """Per-domain backend mix and content length distribution."""
    paths = sorted((raw_dir / "news").glob("*.jsonl"))
    domains: Dict[str, Dict[str, Any]] = {}
    total = 0

    for record in _iter_jsonl(paths):
        total += 1
        domain = urlparse(record.get("url") or "").netloc.replace("www.", "").lower()
        if not domain:
            continue
        entry = domains.setdefault(
            domain,
            {"total": 0, "fast": 0, "slow": 0, "low": 0, "unlabelled": 0, "backends": {}, "lengths": []},
        )
        backend = record.get("backend") or "none"
        entry["total"] += 1
        # Records written before quality tagging existed carry no label; counting
        # them as "ok" would hide the backlog, so they get their own bucket.
        quality = record.get("quality")
        if quality is None:
            entry["unlabelled"] += 1
        elif quality == "low":
            entry["low"] += 1
        entry["backends"][backend] = entry["backends"].get(backend, 0) + 1
        if backend in FAST_BACKENDS:
            entry["fast"] += 1
        else:
            entry["slow"] += 1
        entry["lengths"].append(len(record.get("content") or ""))

    rows = []
    for domain, entry in domains.items():
        lengths = entry["lengths"]
        rows.append(
            {
                "domain": domain,
                "total": entry["total"],
                "slow": entry["slow"],
                "slow_pct": round(entry["slow"] * 100.0 / entry["total"], 1) if entry["total"] else 0.0,
                "low": entry["low"],
                "low_pct": round(entry["low"] * 100.0 / entry["total"], 1) if entry["total"] else 0.0,
                "unlabelled": entry["unlabelled"],
                "unlabelled_pct": round(entry["unlabelled"] * 100.0 / entry["total"], 1) if entry["total"] else 0.0,
                "backends": entry["backends"],
                "chars_median": int(statistics.median(lengths)) if lengths else 0,
                "chars_p10": _percentile(lengths, 0.10),
            }
        )
    rows.sort(key=lambda row: (-row["slow"], -row["total"]))

    return {
        "channel": "news",
        "records": total,
        "slow": sum(row["slow"] for row in rows),
        "domains": rows,
    }


def social_report(raw_dir: Path, platform: str) -> Dict[str, Any]:
    """Per-topic volume and stop reason distribution."""
    paths = sorted((raw_dir / platform).glob("*.jsonl"))
    topics: Dict[str, Dict[str, Any]] = {}
    total = 0

    for record in _iter_jsonl(paths):
        total += 1
        topic = record.get("topic") or "unknown"
        entry = topics.setdefault(
            topic, {"videos": 0, "comments": 0, "replies": 0, "stop_reasons": {}, "truncated": 0}
        )
        entry["videos"] += 1
        entry["comments"] += len(record.get("comments") or [])
        entry["replies"] += int(record.get("replies_fetched") or 0)
        reason = record.get("stop_reason") or "unknown"
        entry["stop_reasons"][reason] = entry["stop_reasons"].get(reason, 0) + 1
        if record.get("truncated"):
            entry["truncated"] += 1

    rows = []
    for topic, entry in topics.items():
        videos = entry["videos"] or 1
        rows.append(
            {
                "topic": topic,
                "videos": entry["videos"],
                "comments": entry["comments"],
                "replies": entry["replies"],
                "comments_per_video": round(entry["comments"] / videos, 1),
                "truncated_pct": round(entry["truncated"] * 100.0 / videos, 1),
                "stop_reasons": entry["stop_reasons"],
            }
        )
    rows.sort(key=lambda row: -row["videos"])
    return {"channel": platform, "records": total, "topics": rows}


def _print_news(report: Dict[str, Any], top: int, min_records: int) -> None:
    rows = [row for row in report["domains"] if row["total"] >= min_records][:top]
    total = report["records"] or 1
    print(f"news: {report['records']} records, {report['slow']} slow ({report['slow'] * 100 // total}%)")
    print()
    print(f"{'domain':<28}{'total':>8}{'slow':>8}{'slow%':>7}{'low%':>7}{'unlab%':>8}{'med chars':>11}{'p10':>8}")
    print("-" * 85)
    for row in rows:
        print(
            f"{row['domain']:<28}{row['total']:>8}{row['slow']:>8}"
            f"{row['slow_pct']:>6}%{row['low_pct']:>6}%{row['unlabelled_pct']:>7}%"
            f"{row['chars_median']:>11}{row['chars_p10']:>8}"
        )


def _print_social(report: Dict[str, Any], top: int) -> None:
    print(f"{report['channel']}: {report['records']} records")
    print()
    print(f"{'topic':<20}{'videos':>8}{'comments':>10}{'replies':>9}{'com/vid':>9}{'trunc%':>8}")
    print("-" * 66)
    for row in report["topics"][:top]:
        print(
            f"{row['topic']:<20}{row['videos']:>8}{row['comments']:>10}"
            f"{row['replies']:>9}{row['comments_per_video']:>9}{row['truncated_pct']:>7}%"
        )
    reasons: Dict[str, int] = {}
    for row in report["topics"]:
        for reason, count in row["stop_reasons"].items():
            reasons[reason] = reasons.get(reason, 0) + count
    if reasons:
        print()
        print("stop reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))


def main() -> int:
    parser = argparse.ArgumentParser(description="Report harvest yield per domain and topic.")
    parser.add_argument("--channel", default="all", choices=["news", "youtube", "twitter", "all"])
    parser.add_argument("--raw-dir", default=str(DEFAULT_RAW_DIR))
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--min-records", type=int, default=300)
    parser.add_argument("--json", action="store_true", help="emit machine readable output")
    args = parser.parse_args()

    raw_dir = Path(args.raw_dir)
    reports: List[Dict[str, Any]] = []
    if args.channel in ("news", "all"):
        reports.append(news_report(raw_dir))
    if args.channel in ("youtube", "all"):
        reports.append(social_report(raw_dir, "youtube"))
    if args.channel in ("twitter", "all"):
        reports.append(social_report(raw_dir, "twitter"))

    if args.json:
        print(json.dumps(reports, indent=2))
        return 0

    for index, report in enumerate(reports):
        if index:
            print()
        if report["channel"] == "news":
            _print_news(report, args.top, args.min_records)
        else:
            _print_social(report, args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
