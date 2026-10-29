"""
Video-level concurrency benchmark for the YouTube harvester.

Pins one video set, then replays it with different `video_workers` values so the
runs do identical work. Also counts InnerTube requests, which is what actually
explains the wall clock: with pacing at 250 ms the ceiling is 4 requests/second,
and no amount of threads goes past it.

    python scripts/bench_videos.py
    python scripts/bench_videos.py --workers 1,4,8 --videos 3 --comments 60
    python scripts/bench_videos.py --videos 3 --unlimited --pacing-ms 100
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from channels import innertube, social_youtube
from channels.social_youtube import YouTubeChannel
from processors.topic_config import TopicConfig
from scripts import social_harvester


def _cfg(workers: int, videos: int, comments: Any, replies: Any, pacing_ms: int) -> TopicConfig:
    return TopicConfig.model_validate({
        "topic": {"id": "bench", "name": "Bench"},
        "keywords": ["timnas"],
        "social": {"enabled": True, "youtube": {
            "queries": ["timnas indonesia"],
            "max_videos_per_run": videos,
            "max_comments_per_video": comments,
            "max_replies_per_comment": replies,
            "request_min_interval_ms": pacing_ms,
            "video_workers": workers,
            "reply_workers": 2,
        }},
    })


def _run_once(
    video_ids: List[str],
    cfg: TopicConfig,
    counter: Dict[str, int],
    gate: threading.Lock,
) -> Dict[str, Any]:
    real_post = social_youtube.post_innertube

    def counting(*args, **kwargs):
        with gate:
            counter["requests"] += 1
        try:
            return real_post(*args, **kwargs)
        except Exception:
            with gate:
                counter["errors"] += 1
            raise

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with patch.object(social_harvester, "YOUTUBE_DIR", root / "youtube"), \
             patch.object(social_harvester, "FRONTIER_TEMPLATE", str(root / "{topic}_{platform}.db")), \
             patch.object(
                 social_harvester,
                 "_youtube_queue",
                 return_value=([(vid, "", "") for vid in video_ids], {"pinned": len(video_ids)}),
             ), \
             patch.object(social_youtube, "post_innertube", side_effect=counting):
            started = time.monotonic()
            result = social_harvester.harvest_youtube(cfg)
            elapsed = time.monotonic() - started

    stop_reasons: Dict[str, int] = {}
    for entry in result.get("coverage", []):
        reason = entry.get("stop_reason") or entry.get("status") or "unknown"
        stop_reasons[reason] = stop_reasons.get(reason, 0) + 1

    return {
        "wall_s": round(elapsed, 2),
        "requests": counter["requests"],
        "req_per_s": round(counter["requests"] / elapsed, 2) if elapsed else 0.0,
        "request_errors": counter["errors"],
        "videos": result.get("videos", 0),
        "comments": result.get("comments", 0),
        "replies": result.get("replies", 0),
        "failed": result.get("failed", 0),
        "truncated": result.get("truncated", 0),
        "stop_reasons": stop_reasons,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark YouTube video concurrency.")
    parser.add_argument("--workers", default="1,4", help="comma separated video_workers values")
    parser.add_argument("--videos", type=int, default=3, help="videos pinned for every run")
    parser.add_argument("--comments", type=int, default=60, help="comment cap per video")
    parser.add_argument("--replies", type=int, default=5, help="reply cap per comment")
    parser.add_argument("--unlimited", action="store_true", help="no comment or reply cap")
    parser.add_argument("--pacing-ms", type=int, default=250, help="InnerTube request spacing")
    args = parser.parse_args()

    comments = None if args.unlimited else args.comments
    replies = None if args.unlimited else args.replies
    workers = [int(value) for value in args.workers.split(",") if value.strip()]

    innertube.refresh_client_version()
    channel = YouTubeChannel()
    hits = channel.search("timnas indonesia", sport="bench", limit=args.videos)
    video_ids = [hit["video_id"] for hit in hits][: args.videos]
    print("pinned:", video_ids, flush=True)
    if not video_ids:
        print("no videos discovered; YouTube is throttling or the query returned nothing", file=sys.stderr)
        return 1

    gate = threading.Lock()
    rows = []
    for worker_count in workers:
        counter = {"requests": 0, "errors": 0}
        row = {"video_workers": worker_count}
        row.update(_run_once(video_ids, _cfg(worker_count, args.videos, comments, replies, args.pacing_ms), counter, gate))
        rows.append(row)
        print(json.dumps(row), flush=True)

    baseline = rows[0]["wall_s"] if rows else 0
    if baseline:
        print()
        for row in rows:
            delta = (row["wall_s"] - baseline) / baseline * 100
            print(f"video_workers={row['video_workers']:<3} {row['wall_s']:>8.2f}s {delta:+6.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
