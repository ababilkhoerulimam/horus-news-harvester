"""
Throughput benchmark for the YouTube harvest path.

Runs the same video across a matrix of transport and reply-worker settings so the
before/after numbers come from one harness instead of ad-hoc timing.

Axes that the code does not support yet are skipped with a note, so the same
script is usable before and after the transport and concurrency changes land.
"""

from __future__ import annotations

import argparse
import inspect
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import channels.social_youtube as youtube_channel
from channels.innertube import configure_pacing, refresh_client_version


def _supports(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _apply_transport(name: str) -> bool:
    """Selects the transport if the module exposes a switch. Returns False when unsupported."""
    configure = getattr(youtube_channel, "configure_transport", None)
    if configure is None:
        from channels import innertube as innertube_module

        configure = getattr(innertube_module, "configure_transport", None)
    if configure is None:
        return name == "urllib"
    configure(name)
    return True


def _run_once(url: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """Harvests one video and records wall time plus request count."""
    stats: Dict[str, Any] = {"requests": 0, "request_seconds": 0.0}
    original = youtube_channel.post_innertube

    def counted(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            stats["requests"] += 1
            stats["request_seconds"] += time.perf_counter() - started

    youtube_channel.post_innertube = counted
    started = time.perf_counter()
    try:
        record = youtube_channel.YouTubeChannel().extract(url, config=config)
    finally:
        youtube_channel.post_innertube = original
    elapsed = time.perf_counter() - started

    comments = record.get("comments") or []
    replies = sum(len(c.get("replies") or []) for c in comments)
    return {
        "elapsed": elapsed,
        "requests": stats["requests"],
        "request_seconds": stats["request_seconds"],
        "comments": len(comments),
        "replies": replies,
        "pages": record.get("pages"),
        "stop_reason": record.get("stop_reason"),
        "status": record.get("status"),
    }


def _fmt_row(label: str, run: Dict[str, Any]) -> str:
    elapsed = run["elapsed"] or 0.0001
    per_request = (run["request_seconds"] / run["requests"] * 1000) if run["requests"] else 0.0
    return (
        f"{label:<28} {elapsed:8.2f}s {run['requests']:>6} req "
        f"{per_request:>8.0f} ms/req {run['comments']:>6} com "
        f"{run['replies']:>6} rep {run['comments'] / elapsed:>7.2f} com/s "
        f"{run['stop_reason']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the YouTube harvest path.")
    parser.add_argument("url", help="YouTube video URL")
    parser.add_argument("--repeat", type=int, default=1, help="runs per matrix cell")
    parser.add_argument("--max-comments", type=int, default=300)
    parser.add_argument("--max-replies", type=int, default=50)
    parser.add_argument("--unlimited", action="store_true", help="ignore both record caps")
    parser.add_argument("--workers", default="1,4", help="comma separated reply_workers values")
    parser.add_argument("--transports", default="urllib,curl", help="comma separated transport names")
    parser.add_argument(
        "--pacing-ms",
        type=int,
        default=0,
        help="minimum gap between InnerTube requests, mirrors request_min_interval_ms",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    max_comments: Optional[int] = None if args.unlimited else args.max_comments
    max_replies: Optional[int] = None if args.unlimited else args.max_replies

    refresh_client_version()
    configure_pacing(args.pacing_ms / 1000.0)

    supports_workers = _supports(youtube_channel.YouTubeChannel._fetch_innertube_comments, "reply_workers")
    workers = [int(w) for w in args.workers.split(",") if w.strip()]
    if not supports_workers:
        print("note: reply_workers not wired yet, running serial only")
        workers = [1]

    print(f"video {args.url}")
    print(
        f"caps  max_comments={max_comments} max_replies={max_replies} "
        f"repeat={args.repeat} pacing={args.pacing_ms}ms"
    )
    print("-" * 118)

    for transport in [t.strip() for t in args.transports.split(",") if t.strip()]:
        if not _apply_transport(transport):
            print(f"note: transport {transport!r} not available yet, skipped")
            continue
        for worker_count in workers:
            config: Dict[str, Any] = {
                "max_comments": max_comments,
                "max_replies_per_comment": max_replies,
                "reply_workers": worker_count,
            }
            for index in range(args.repeat):
                run = _run_once(args.url, config)
                label = f"{transport}/workers={worker_count}#{index + 1}"
                print(_fmt_row(label, run))

    print("-" * 118)
    print("com/s = comments collected per wall second; ms/req = mean time inside post_innertube")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
