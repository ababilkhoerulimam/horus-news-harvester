"""Social harvest orchestration for YouTube and Twitter."""

import json
import logging
import time
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from channels.innertube import (
    configure_cookies,
    configure_pacing,
    refresh_client_version,
    sapisid_hash,
    visitor_data,
)
from processors import session_store
from processors.http_client import DEFAULT_USER_AGENTS, default_http_client
from channels.social_twitter import TwitterChannel
from channels.social_youtube import YouTubeChannel
from processors.crawl_frontier import CrawlFrontier
from processors.topic_config import TopicConfig, load_topic_config

logger = logging.getLogger("social_harvester")

YOUTUBE_DIR = Path("data/raw/youtube")
TWITTER_DIR = Path("data/raw/twitter")
FRONTIER_TEMPLATE = "data/state/social_{topic}_{platform}_frontier.db"


def _frontier(topic_id: str, platform: str) -> CrawlFrontier:
    return CrawlFrontier(FRONTIER_TEMPLATE.format(topic=topic_id, platform=platform), batch_size=1)


def _append_jsonl(path: Path, records: List[Dict[str, Any]]) -> int:
    """Appends records to a JSONL file, creating the parent directory."""
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()
    return len(records)


def _youtube_queue(
    cfg: TopicConfig,
    channel: YouTubeChannel,
    frontier: CrawlFrontier,
    cap: int,
    resume: bool,
) -> Tuple[List[Tuple[str, str, str]], Dict[str, int]]:
    """Returns the work queue for this run plus per-query discovery counts."""
    if resume:
        pending = frontier.get_pending_batch(limit=cap, portal="youtube")
        queue = [(item["url"], item.get("title", ""), item.get("published_date", "")) for item in pending]
        return queue, {"resume": len(queue)}

    social = cfg.social.youtube
    queue: List[Tuple[str, str, str]] = []
    discovered: Dict[str, int] = {}
    seen: set = set()

    for query in social.queries:
        hits = channel.search(query, sport=cfg.topic.id, limit=cap)
        discovered[query] = len(hits)
        for item in hits:
            video_id = item.get("video_id")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            candidate = {
                "url": video_id,
                "title": item.get("title", ""),
                "published_date": item.get("published_date", ""),
            }
            if frontier.add_candidates([candidate], portal="youtube"):
                queue.append((video_id, candidate["title"], candidate["published_date"]))
            if len(queue) >= cap:
                return queue, discovered

    return queue, discovered


def _progress_logger(video_id: str, every: int):
    """Returns a callback that logs comment progress at most once per interval."""
    state = {"comments": 0}

    def report(stats: Dict[str, Any]) -> None:
        if stats["comments"] - state["comments"] < every:
            return
        state["comments"] = stats["comments"]
        logger.info(
            "[youtube] %s progress: %s comments, %s replies, %s pages",
            video_id,
            stats["comments"],
            stats["replies"],
            stats["pages"],
        )

    return report


def harvest_youtube(
    cfg: TopicConfig,
    resume: bool = False,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Discovers videos and harvests their full comment threads."""
    social = cfg.social.youtube
    if not social.queries and not social.channels:
        return {"platform": "youtube", "status": "skipped", "reason": "no targets"}

    # Load the session before refreshing, so the page fetch that supplies the
    # client version and visitor id is the same identity as the InnerTube calls.
    cookies = session_store.load("youtube.com")
    if cookies:
        configure_cookies(cookies)
        # Cookies and User-Agent must agree. Without a captured UA, use the one
        # that matches curl_cffi's chrome124 TLS fingerprint.
        user_agent = session_store.load_user_agent("youtube.com") or DEFAULT_USER_AGENTS[0]
        default_http_client.set_user_agent(user_agent)
        logger.info("[youtube] cookie session loaded (%s cookies)", len(cookies))
    else:
        default_http_client.set_user_agent(None)

    refresh_client_version()
    configure_pacing(social.request_min_interval_ms / 1000.0)
    if cookies:
        logger.info(
            "[youtube] identity: visitor=%s signed=%s",
            "yes" if visitor_data() else "no",
            "yes" if sapisid_hash() else "no",
        )
    channel = YouTubeChannel()
    frontier = _frontier(cfg.topic.id, "youtube")
    if resume:
        frontier.reset_in_progress()

    cap = limit or social.max_videos_per_run
    queue, discovered = _youtube_queue(cfg, channel, frontier, cap, resume)
    runtime = social.max_runtime_seconds
    logger.info(
        "[youtube] budget: %s videos, %s comments/video, %s replies/comment, runtime %s",
        cap,
        social.max_comments_per_video,
        social.max_replies_per_comment,
        f"{runtime}s" if runtime else "unlimited",
    )
    logger.info("[youtube] %s videos queued for topic %s", len(queue), cfg.topic.id)

    records: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []
    failed = 0
    truncated = 0
    started = time.monotonic()
    deadline = started + runtime if runtime else None
    processed = 0

    def _extract_video(video_id: str) -> Dict[str, Any]:
        """Runs one video's comment thread. Called on a worker thread."""
        try:
            return channel.extract(
                f"https://www.youtube.com/watch?v={video_id}",
                config={
                    "max_comments": social.max_comments_per_video,
                    "max_replies_per_comment": social.max_replies_per_comment,
                    "reply_workers": social.reply_workers,
                    "deadline": deadline,
                    "on_progress": _progress_logger(video_id, social.progress_every),
                },
            )
        except Exception as exc:
            logger.warning("[youtube] %s raised: %s", video_id, exc)
            return {"status": "failed", "error": str(exc)}

    def _record_video(video_id: str, result: Dict[str, Any]) -> None:
        """Bookkeeping for one finished video. Runs on the main thread only."""
        nonlocal failed, truncated
        if result.get("status") == "success":
            result["sport"] = cfg.topic.id
            records.append(result)
            comments = result.get("comments", [])
            entry = {
                "video_id": video_id,
                "status": "success",
                "comments": len(comments),
                "replies": sum(len(c.get("replies", [])) for c in comments),
                "pages": result.get("pages", 0),
                "stop_reason": result.get("stop_reason", "exhausted"),
            }
            if result.get("truncated"):
                truncated += 1
                entry["status"] = "truncated"
                frontier.mark_failed(video_id, error=f"truncated:{result.get('stop_reason')}")
            else:
                frontier.mark_completed(video_id)
            coverage.append(entry)
        else:
            failed += 1
            frontier.mark_failed(video_id, error=result.get("error", "extraction failed"))
            coverage.append({
                "video_id": video_id,
                "status": "failed",
                "error": result.get("error", "extraction failed"),
            })
        frontier.flush()

    # Videos run concurrently, but the frontier stays on this thread: it is one
    # SQLite connection, and a shared connection across threads serialises anyway
    # while risking "database is locked". Only the network work is parallel.
    #
    # Pacing still caps the request rate, so this does not raise load on YouTube.
    # It removes the idle gap between one video's requests and the next, which is
    # where a single-threaded run spends most of its wall clock.
    video_workers = max(1, social.video_workers)
    executor = ThreadPoolExecutor(max_workers=video_workers, thread_name_prefix="yt-video")
    in_flight: Dict[Future, str] = {}
    pending = iter(queue)
    queue_drained = False

    try:
        while True:
            while not queue_drained and len(in_flight) < video_workers:
                if deadline is not None and time.monotonic() >= deadline:
                    logger.warning(
                        "[youtube] runtime budget reached after %s videos, %s left queued",
                        processed,
                        len(queue) - processed,
                    )
                    queue_drained = True
                    break
                item = next(pending, None)
                if item is None:
                    queue_drained = True
                    break
                video_id = item[0]
                processed += 1
                frontier.mark_in_progress([video_id])
                in_flight[executor.submit(_extract_video, video_id)] = video_id

            if not in_flight:
                break

            finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
            for future in finished:
                video_id = in_flight.pop(future)
                _record_video(video_id, future.result())
    finally:
        executor.shutdown(wait=True)

    written = _append_jsonl(YOUTUBE_DIR / f"{cfg.topic.id}.jsonl", records)
    frontier.close()

    comment_total = sum(len(r.get("comments", [])) for r in records)
    reply_total = sum(item.get("replies", 0) for item in coverage)
    return {
        "platform": "youtube",
        "status": "success",
        "videos": written,
        "comments": comment_total,
        "replies": reply_total,
        "failed": failed,
        "truncated": truncated,
        "discovered": discovered,
        "coverage": coverage,
    }


def harvest_twitter(
    cfg: TopicConfig,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Searches queries and handles, writing only tweets not already recorded.

    Resume is implicit: the frontier deduplicates by tweet id, so re-running
    after an interruption picks up only unseen tweets.
    """
    social = cfg.social.twitter
    if not social.queries and not social.handles:
        return {"platform": "twitter", "status": "skipped", "reason": "no targets"}

    channel = TwitterChannel()
    frontier = _frontier(cfg.topic.id, "twitter")
    per_target = limit or social.max_tweets_per_query

    targets: List[Tuple[str, str]] = [
        (query, "query") for query in social.queries
    ] + [(handle, "handle") for handle in social.handles]

    records: List[Dict[str, Any]] = []
    coverage: List[Dict[str, Any]] = []
    seen: set = set()
    failed = 0
    logger.info("[twitter] budget: %s items per target, %s targets", per_target, len(targets))

    for target, kind in targets:
        try:
            if kind == "query":
                tweets = channel.search(target, sport=cfg.topic.id, limit=per_target)
            else:
                tweets = channel.get_user_tweets(target, sport=cfg.topic.id, limit=per_target)
        except Exception as exc:
            logger.warning("[twitter] %s failed: %s", target, exc)
            failed += 1
            coverage.append({"target": target, "kind": kind, "status": "failed", "error": str(exc)})
            continue

        added = 0
        for tweet in tweets:
            tweet_id = tweet.get("tweet_id")
            if not tweet_id or tweet_id in seen:
                continue
            seen.add(tweet_id)
            candidate = {
                "url": tweet_id,
                "title": tweet.get("title", ""),
                "published_date": tweet.get("published_date", ""),
            }
            if frontier.add_candidates([candidate], portal="twitter"):
                records.append(tweet)
                frontier.mark_completed(tweet_id)
                added += 1
        coverage.append({
            "target": target,
            "kind": kind,
            "status": "success",
            "discovered": len(tweets),
            "new": added,
        })
        frontier.flush()

    written = _append_jsonl(TWITTER_DIR / f"{cfg.topic.id}.jsonl", records)
    frontier.close()

    return {
        "platform": "twitter",
        "status": "success",
        "tweets": written,
        "failed": failed,
        "coverage": coverage,
    }


def run_social_harvest(
    topic_name: str,
    platform: str = "both",
    resume: bool = False,
    limit: Optional[int] = None,
    max_runtime: Optional[int] = None,
) -> Dict[str, Any]:
    """Runs the configured social harvest for one topic."""
    cfg = load_topic_config(topic_name)
    if cfg.social is None or not cfg.social.enabled:
        return {"status": "skipped", "reason": "social harvesting disabled for this topic"}

    if max_runtime is not None:
        cfg.social.youtube.max_runtime_seconds = max_runtime

    results: List[Dict[str, Any]] = []
    if platform in ("both", "youtube"):
        results.append(harvest_youtube(cfg, resume=resume, limit=limit))
    if platform in ("both", "twitter"):
        results.append(harvest_twitter(cfg, limit=limit))

    return {"status": "success", "platforms": results}
