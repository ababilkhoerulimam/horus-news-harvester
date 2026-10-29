"""
YouTube Sports Channel & Comment Harvester.
Implements multi-backend fallback: InnerTube API -> yt-dlp -> Jina Reader.
Extracts video metadata and fan comments for sentiment & discourse analysis.
"""

import json
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .base import SportsChannel
from .innertube import post_innertube
from backends.jina_reader import JinaReaderBackend
from processors.cleaner import clean_news_text
from processors.metrics import parse_count
from processors.youtube_normalizer import build_comment, build_video_record


INNERTUBE_URL_SEARCH = "https://www.youtube.com/youtubei/v1/search"
INNERTUBE_URL_NEXT = "https://www.youtube.com/youtubei/v1/next"
logger = logging.getLogger("social_youtube")

MAX_SEARCH_PAGES = 5

HARD_COMMENT_PAGES = 5000
HARD_REPLY_PAGES = 500

STOP_EXHAUSTED = "exhausted"
STOP_MAX_COMMENTS = "max_comments"
STOP_MAX_REPLIES = "max_replies"
STOP_RUNTIME = "runtime_budget"
STOP_NO_PROGRESS = "no_progress"
STOP_REPEATED_TOKEN = "repeated_token"
STOP_SAFETY_VALVE = "safety_valve"
STOP_PAGE_ERROR = "page_error"


@dataclass
class CommentHarvest:
    """Comments collected from one video, with the reason the walk stopped."""
    comments: List[Dict[str, Any]]
    stop_reason: str
    pages: int
    replies_fetched: int

WEB_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# ponytail: Innertube unauthenticated client payload; add OAuth/API key if quota or consent wall encountered.
INNERTUBE_CLIENT = {
    "context": {
        "client": {
            "clientName": "WEB",
            "clientVersion": "2.20240101.00.00",
            "hl": "id",
            "gl": "ID",
        }
    }
}


def _find_continuation_token(node):
    """Returns the first continuation token found anywhere in a response tree."""
    if isinstance(node, dict):
        command = node.get("continuationCommand")
        if isinstance(command, dict) and command.get("token"):
            return command["token"]
        for value in node.values():
            token = _find_continuation_token(value)
            if token:
                return token
    elif isinstance(node, list):
        for value in node:
            token = _find_continuation_token(value)
            if token:
                return token
    return None


def _reply_meta(thread_renderer: Dict[str, Any]) -> Tuple[Optional[str], int]:
    """Returns the reply continuation token and the advertised reply count."""
    replies = thread_renderer.get("replies", {}).get("commentRepliesRenderer", {})
    token = None
    for item in replies.get("contents", []):
        if "continuationItemRenderer" not in item:
            continue
        token = _find_continuation_token(item)
        if token:
            break

    count = 0
    button = replies.get("viewReplies", {}).get("buttonRenderer", {})
    for run in button.get("text", {}).get("runs", []):
        count = parse_count(run.get("text"))
        if count:
            break
    return token, count


def _reply_meta_by_comment(page_json: Dict[str, Any]) -> Dict[str, Tuple[Optional[str], int]]:
    """Maps root comment ids to their reply token and advertised reply count."""
    meta: Dict[str, Tuple[Optional[str], int]] = {}
    for ep in page_json.get("onResponseReceivedEndpoints", []):
        cmd = (
            ep.get("reloadContinuationItemsCommand")
            or ep.get("appendContinuationItemsCommand")
            or ep.get("appendContinuationItemsAction")
        )
        if not cmd:
            continue
        for item in cmd.get("continuationItems", []):
            thread = item.get("commentThreadRenderer")
            if not thread:
                continue
            comment_id = thread.get("commentViewModel", {}).get("commentViewModel", {}).get("commentId")
            if comment_id:
                meta[comment_id] = _reply_meta(thread)
    return meta


class YouTubeChannel(SportsChannel):
    """Channel handling Indonesian sports YouTube videos and comment sections."""

    name = "social_youtube"
    description = "Komentar dan Diskusi Video Olahraga YouTube Indonesia"
    backends = ["innertube_api", "yt_dlp", "jina_reader"]
    tier = 0

    def __init__(self):
        self.jina = JinaReaderBackend()

    def can_handle(self, url_or_source: str) -> bool:
        url_lower = url_or_source.lower()
        return any(x in url_lower for x in ("youtube.com", "youtu.be", "@"))

    def check(self, config: Optional[Dict[str, Any]] = None) -> Tuple[str, str]:
        # Test if yt-dlp is available locally
        yt_dlp_ok = False
        try:
            res = subprocess.run(["yt-dlp", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if res.returncode == 0:
                yt_dlp_ok = True
        except Exception as exc:
            logger.debug("yt-dlp probe failed: %s", exc)

        self.active_backend = "innertube_api"
        status_msg = f"Active: innertube_api, yt-dlp: {'available' if yt_dlp_ok else 'not found'}"
        return "ok", status_msg

    def _extract_video_id(self, url: str) -> Optional[str]:
        match = re.search(r"(?:v=|\/embed\/|\/watch\?v=|youtu\.be\/|\/v\/)([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

    def _parse_search_page(
        self,
        page_json: Dict[str, Any],
        sport: str,
        limit: int,
        results: List[Dict[str, Any]],
    ) -> None:
        """Appends video stubs from one search page, stopping at limit."""
        contents = (
            page_json.get("contents", {})
            .get("twoColumnSearchResultsRenderer", {})
            .get("primaryContents", {})
            .get("sectionListRenderer", {})
            .get("contents", [])
        )
        for sec in contents:
            item_section = sec.get("itemSectionRenderer", {}).get("contents", [])
            for item in item_section:
                video = item.get("videoRenderer")
                if not video:
                    continue
                vid_id = video.get("videoId")
                if not vid_id:
                    continue
                title_runs = video.get("title", {}).get("runs", [])
                title = "".join(r.get("text", "") for r in title_runs)
                channel_runs = video.get("ownerText", {}).get("runs", [])
                author = "".join(r.get("text", "") for r in channel_runs)
                published = video.get("publishedTimeText", {}).get("simpleText", "")
                results.append({
                    "id": vid_id,
                    "video_id": vid_id,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid_id}",
                    "author": author,
                    "published_date": published,
                    "sport": sport,
                    "source": "youtube",
                    "backend": "innertube_api",
                })
                if len(results) >= limit:
                    return

    def search(
        self,
        query: str,
        sport: str = "football",
        limit: int = 10,
        config: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        """Searches YouTube videos matching sport keywords via InnerTube API."""
        backends_to_try = self.ordered_backends(config)
        results = []

        for backend in backends_to_try:
            if backend == "innertube_api":
                try:
                    payload = dict(INNERTUBE_CLIENT)
                    payload["query"] = query
                    page_json = post_innertube(INNERTUBE_URL_SEARCH, payload, timeout=12)
                    if page_json is None:
                        # A blocked request and a query with no matches both end up
                        # here, and "0 videos queued" reads like the second one.
                        logger.warning(
                            "InnerTube search returned no page for %r (blocked, throttled, or empty)",
                            query,
                        )
                    page = 0
                    while page_json is not None and len(results) < limit and page < MAX_SEARCH_PAGES:
                        page += 1
                        self._parse_search_page(page_json, sport, limit, results)
                        if len(results) >= limit:
                            break
                        token = _find_continuation_token(page_json)
                        if not token:
                            break
                        payload = dict(INNERTUBE_CLIENT)
                        payload["continuation"] = token
                        page_json = post_innertube(INNERTUBE_URL_SEARCH, payload, timeout=12)
                    if results:
                        return results
                except Exception as exc:
                    logger.warning("InnerTube search failed for %r: %s", query, exc)
                    continue

            elif backend == "yt_dlp":
                try:
                    cmd = [
                        "yt-dlp",
                        f"ytsearch{limit}:{query}",
                        "--dump-single-json",
                        "--flat-playlist",
                        "--skip-download",
                    ]
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
                    if proc.returncode == 0:
                        data = json.loads(proc.stdout)
                        for entry in data.get("entries", []):
                            vid_id = entry.get("id")
                            results.append({
                                "id": vid_id,
                                "video_id": vid_id,
                                "title": entry.get("title", ""),
                                "url": f"https://www.youtube.com/watch?v={vid_id}",
                                "author": entry.get("uploader", ""),
                                "published_date": entry.get("upload_date", ""),
                                "sport": sport,
                                "source": "youtube",
                                "backend": "yt_dlp",
                            })
                            if len(results) >= limit:
                                return results
                    if results:
                        return results
                except Exception as exc:
                    logger.warning("[youtube] yt-dlp search failed for %r: %s", query, exc)
                    continue

        return results

    def _parse_innertube_comments_page(
        self,
        page_json: Dict[str, Any],
        budget: Optional[int],
        seen_ids: set
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Parses viewer comments and next continuation token from an InnerTube next response.
        Supports both modern entity updates (commentEntityPayload) and legacy commentThreadRenderer.
        Extracts nested reply continuation tokens when available.
        """
        comments: List[Dict[str, Any]] = []
        reply_meta = _reply_meta_by_comment(page_json)

        # 1. Modern YouTube entity updates
        mutations = page_json.get("frameworkUpdates", {}).get("entityBatchUpdate", {}).get("mutations", [])
        for m in mutations:
            cep = m.get("payload", {}).get("commentEntityPayload")
            if not cep:
                continue
            props = cep.get("properties", {})
            cid = props.get("commentId")
            if not cid or cid in seen_ids:
                continue

            seen_ids.add(cid)
            content_text = props.get("content", {}).get("content", "")
            author = cep.get("author", {}).get("displayName", "Anonymous")
            toolbar = cep.get("toolbar", {})
            likes = toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or "0"
            published = props.get("publishedTime", "")

            reply_token, reply_count = reply_meta.get(cid, (None, 0))
            entry = build_comment(cid, author, content_text, likes, published, reply_count=reply_count)
            if reply_token:
                entry["_reply_token"] = reply_token
            comments.append(entry)
            if budget is not None and len(comments) >= budget:
                break

        # 2. Legacy commentThreadRenderer / commentRenderer fallback
        if not comments:
            for ep in page_json.get("onResponseReceivedEndpoints", []):
                cmd = (
                    ep.get("reloadContinuationItemsCommand")
                    or ep.get("appendContinuationItemsCommand")
                    or ep.get("appendContinuationItemsAction")
                )
                if not cmd:
                    continue
                for item in cmd.get("continuationItems", []):
                    ctr = item.get("commentThreadRenderer")
                    if not ctr:
                        continue
                    cr = ctr.get("comment", {}).get("commentRenderer")
                    if not cr:
                        continue
                    cid = cr.get("commentId")
                    if not cid or cid in seen_ids:
                        continue

                    seen_ids.add(cid)
                    text_runs = cr.get("contentText", {}).get("runs", [])
                    text = "".join(r.get("text", "") for r in text_runs)
                    author = cr.get("authorText", {}).get("simpleText") or "".join(r.get("text", "") for r in cr.get("authorText", {}).get("runs", [])) or "Anonymous"
                    likes = cr.get("voteCount", {}).get("simpleText", "0")
                    published = cr.get("publishedTimeText", {}).get("simpleText", "")

                    reply_token, reply_count = reply_meta.get(cid) or _reply_meta(ctr)
                    c_entry = build_comment(cid, author, text, likes, published, reply_count=reply_count)
                    if reply_token:
                        c_entry["_reply_token"] = reply_token

                    comments.append(c_entry)
                    if budget is not None and len(comments) >= budget:
                        break

        # 3. Discover next continuation token
        next_token = None
        for ep in page_json.get("onResponseReceivedEndpoints", []):
            cmd = (
                ep.get("reloadContinuationItemsCommand")
                or ep.get("appendContinuationItemsCommand")
                or ep.get("appendContinuationItemsAction")
            )
            if not cmd:
                continue
            for item in cmd.get("continuationItems", []):
                if "continuationItemRenderer" not in item:
                    continue
                token = _find_continuation_token(item)
                if token:
                    next_token = token
                    break
            if next_token:
                break

        return comments, next_token

    def _parse_reply_page(
        self,
        page_json: Dict[str, Any],
        seen_ids: set,
        parent_id: Optional[str],
        depth: int,
        budget: Optional[int],
    ) -> Tuple[List[Dict[str, Any]], List[str]]:
        """Parses one reply page into records and the follow-up continuation tokens."""
        records: List[Dict[str, Any]] = []

        mutations = page_json.get("frameworkUpdates", {}).get("entityBatchUpdate", {}).get("mutations", [])
        for m in mutations:
            cep = m.get("payload", {}).get("commentEntityPayload")
            if not cep:
                continue
            props = cep.get("properties", {})
            cid = props.get("commentId")
            if not cid or cid in seen_ids:
                continue
            seen_ids.add(cid)
            toolbar = cep.get("toolbar", {})
            records.append(build_comment(
                cid,
                cep.get("author", {}).get("displayName"),
                props.get("content", {}).get("content", ""),
                toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or "0",
                props.get("publishedTime", ""),
                parent_id=parent_id,
                depth=depth,
            ))
            if budget is not None and len(records) >= budget:
                break

        if not records:
            for ep in page_json.get("onResponseReceivedEndpoints", []):
                cmd = (
                    ep.get("appendContinuationItemsAction")
                    or ep.get("appendContinuationItemsCommand")
                    or ep.get("reloadContinuationItemsCommand")
                )
                if not cmd:
                    continue
                for item in cmd.get("continuationItems", []):
                    cr = item.get("commentRenderer")
                    if not cr:
                        continue
                    cid = cr.get("commentId")
                    if not cid or cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    text_runs = cr.get("contentText", {}).get("runs", [])
                    text = "".join(r.get("text", "") for r in text_runs)
                    author = (
                        cr.get("authorText", {}).get("simpleText")
                        or "".join(r.get("text", "") for r in cr.get("authorText", {}).get("runs", []))
                    )
                    records.append(build_comment(
                        cid,
                        author,
                        text,
                        cr.get("voteCount", {}).get("simpleText", "0"),
                        cr.get("publishedTimeText", {}).get("simpleText", ""),
                        parent_id=parent_id,
                        depth=depth,
                    ))
                    if budget is not None and len(records) >= budget:
                        break

        follow_ups: List[str] = []
        for ep in page_json.get("onResponseReceivedEndpoints", []):
            cmd = (
                ep.get("appendContinuationItemsAction")
                or ep.get("appendContinuationItemsCommand")
                or ep.get("reloadContinuationItemsCommand")
            )
            if not cmd:
                continue
            for item in cmd.get("continuationItems", []):
                if "continuationItemRenderer" not in item:
                    continue
                token = _find_continuation_token(item)
                if token:
                    follow_ups.append(token)

        return records, follow_ups

    def _fetch_innertube_replies(
        self,
        reply_token: Optional[str],
        max_replies: Optional[int] = None,
        parent_id: Optional[str] = None,
        max_pages: Optional[int] = None,
        deadline: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Follows every continuation token a reply thread exposes until it ends.

        max_replies caps the records kept. max_pages caps requests. deadline is an
        absolute time.monotonic() value. When max_replies is set and max_pages is not,
        the page budget scales with the record budget so a capped call still terminates.
        """
        if not reply_token:
            return []

        if max_pages is None and max_replies is not None:
            max_pages = max(2, min(50, (max_replies + 9) // 10))

        replies: List[Dict[str, Any]] = []
        seen_reply_ids: set = set()
        seen_tokens: set = set()
        pending = [reply_token]
        pages = 0

        while pending:
            if max_replies is not None and len(replies) >= max_replies:
                break
            if max_pages is not None and pages >= max_pages:
                break
            if deadline is not None and time.monotonic() >= deadline:
                break
            if pages >= HARD_REPLY_PAGES:
                logger.warning("[youtube] safety valve at %s reply pages for %s", pages, parent_id)
                break

            token = pending.pop(0)
            if token in seen_tokens:
                continue
            seen_tokens.add(token)
            pages += 1

            payload = dict(INNERTUBE_CLIENT)
            payload["continuation"] = token
            try:
                page_json = post_innertube(INNERTUBE_URL_NEXT, payload, timeout=10)
            except Exception as exc:
                logger.warning("[youtube] reply page %s failed for %s: %s", pages, parent_id, exc)
                break
            if page_json is None:
                break

            remaining = None if max_replies is None else max_replies - len(replies)
            records, follow_ups = self._parse_reply_page(page_json, seen_reply_ids, parent_id, 1, remaining)
            replies.extend(records)
            pending.extend(follow_ups)

        return replies

    def _safe_replies(
        self,
        reply_token: str,
        max_replies: Optional[int],
        parent_id: str,
        deadline: Optional[float],
    ) -> List[Dict[str, Any]]:
        """Worker entry point: one failing thread must not take the whole pool down."""
        try:
            return self._fetch_innertube_replies(
                reply_token,
                max_replies=max_replies,
                parent_id=parent_id,
                deadline=deadline,
            )
        except Exception as exc:
            logger.warning("[youtube] reply fetch failed for %s: %s", parent_id, exc)
            return []

    def _fetch_innertube_comments(
        self,
        init_token: Optional[str],
        max_comments: Optional[int] = None,
        max_replies_per_comment: Optional[int] = None,
        fetch_replies: bool = True,
        deadline: Optional[float] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        reply_workers: int = 1,
    ) -> CommentHarvest:
        """Walks the comment continuation chain until it ends or a guard fires.

        None means unlimited for both budgets. The loop always has a termination
        path: empty token, budget, deadline, two empty pages, a repeated token, or
        the safety valve. Without the last three an uncapped run can spin forever
        on a cursor that keeps returning the same page.

        reply_workers above 1 fetches reply threads from a pool. Each page is drained
        before the next one is requested, so memory stays bounded and the reply order
        stays deterministic regardless of worker count.
        """
        if not init_token:
            return CommentHarvest([], STOP_EXHAUSTED, 0, 0)

        collected: List[Dict[str, Any]] = []
        seen_ids: set = set()
        seen_tokens: set = set()
        token: Optional[str] = init_token
        pages = 0
        empty_pages = 0
        replies_fetched = 0
        stop_reason = STOP_EXHAUSTED
        executor = (
            ThreadPoolExecutor(max_workers=reply_workers)
            if fetch_replies and reply_workers > 1
            else None
        )

        try:
            while token:
                if deadline is not None and time.monotonic() >= deadline:
                    stop_reason = STOP_RUNTIME
                    break
                if max_comments is not None and len(collected) >= max_comments:
                    stop_reason = STOP_MAX_COMMENTS
                    break
                if pages >= HARD_COMMENT_PAGES:
                    logger.warning("[youtube] safety valve at %s comment pages", pages)
                    stop_reason = STOP_SAFETY_VALVE
                    break
                if token in seen_tokens:
                    logger.warning("[youtube] repeated comment token at page %s", pages)
                    stop_reason = STOP_REPEATED_TOKEN
                    break

                seen_tokens.add(token)
                pages += 1
                payload = dict(INNERTUBE_CLIENT)
                payload["continuation"] = token
                try:
                    page_json = post_innertube(INNERTUBE_URL_NEXT, payload, timeout=12, use_jitter=True)
                except Exception as exc:
                    logger.warning("[youtube] comment page %s failed: %s", pages, exc)
                    stop_reason = STOP_PAGE_ERROR
                    break
                if page_json is None:
                    stop_reason = STOP_PAGE_ERROR
                    break

                remaining = None if max_comments is None else max_comments - len(collected)
                page_comments, next_token = self._parse_innertube_comments_page(page_json, remaining, seen_ids)

                pending_jobs: List[Tuple[Dict[str, Any], Any]] = []
                for c in page_comments:
                    reply_tok = c.pop("_reply_token", None)
                    if not (fetch_replies and reply_tok):
                        continue
                    if executor is None:
                        c["replies"] = self._fetch_innertube_replies(
                            reply_tok,
                            max_replies=max_replies_per_comment,
                            parent_id=c["id"],
                            deadline=deadline,
                        )
                        c["reply_count"] = max(c.get("reply_count", 0), len(c["replies"]))
                        replies_fetched += len(c["replies"])
                    else:
                        pending_jobs.append(
                            (
                                c,
                                executor.submit(
                                    self._safe_replies,
                                    reply_tok,
                                    max_replies_per_comment,
                                    c["id"],
                                    deadline,
                                ),
                            )
                        )

                for c, future in pending_jobs:
                    c["replies"] = future.result()
                    c["reply_count"] = max(c.get("reply_count", 0), len(c["replies"]))
                    replies_fetched += len(c["replies"])

                collected.extend(page_comments)
                empty_pages = 0 if page_comments else empty_pages + 1
                if empty_pages >= 2:
                    stop_reason = STOP_NO_PROGRESS
                    break

                token = next_token
                if on_progress is not None:
                    on_progress({"comments": len(collected), "replies": replies_fetched, "pages": pages})
        finally:
            if executor is not None:
                executor.shutdown(wait=True)

        return CommentHarvest(collected, stop_reason, pages, replies_fetched)

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extracts video details and top viewer comments with nested replies."""
        backends_to_try = self.ordered_backends(config)
        video_id = self._extract_video_id(url)
        max_comments = config.get("max_comments", 50) if config else 50
        max_replies_per_comment = config.get("max_replies_per_comment", 10) if config else 10
        reply_workers = config.get("reply_workers", 1) if config else 1
        deadline = config.get("deadline") if config else None
        on_progress = config.get("on_progress") if config else None

        if not video_id:
            return {"url": url, "status": "failed", "error": "Invalid YouTube video URL", "backend": "none"}

        for backend in backends_to_try:
            if backend == "innertube_api":
                try:
                    payload = dict(INNERTUBE_CLIENT)
                    payload["videoId"] = video_id
                    res_json = post_innertube(INNERTUBE_URL_NEXT, payload, timeout=12)
                    if res_json is None:
                        continue

                    # Extract video title, description, and initial comments continuation token
                    title = ""
                    description = ""
                    init_token = None
                    try:
                        results_cont = res_json.get("contents", {}).get("twoColumnWatchNextResults", {}).get("results", {}).get("results", {}).get("contents", [])
                        for c in results_cont:
                            item = c.get("videoPrimaryInfoRenderer")
                            if item:
                                title = "".join(r.get("text", "") for r in item.get("title", {}).get("runs", []))
                            sec = c.get("videoSecondaryInfoRenderer")
                            if sec:
                                attr_desc = sec.get("attributedDescription", {})
                                if isinstance(attr_desc, dict):
                                    description = attr_desc.get("content", "")
                                    if not description and "runs" in attr_desc:
                                        description = "".join(r.get("text", "") for r in attr_desc.get("runs", []))
                                elif isinstance(attr_desc, str):
                                    description = attr_desc
                            isr = c.get("itemSectionRenderer")
                            if isr:
                                for sub in isr.get("contents", []):
                                    cir = sub.get("continuationItemRenderer")
                                    if cir:
                                        t = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                                        if t:
                                            init_token = t
                                            break
                    except Exception as exc:
                        logger.warning("[youtube] metadata parse failed for %s: %s", video_id, exc)

                    harvest = self._fetch_innertube_comments(
                        init_token,
                        max_comments=max_comments,
                        max_replies_per_comment=max_replies_per_comment,
                        deadline=deadline,
                        on_progress=on_progress,
                        reply_workers=reply_workers,
                    )
                    record = build_video_record(
                        video_id,
                        url,
                        title,
                        clean_news_text(description or title),
                        harvest.comments,
                        "innertube_api",
                    )
                    record["stop_reason"] = harvest.stop_reason
                    record["truncated"] = harvest.stop_reason != STOP_EXHAUSTED
                    record["pages"] = harvest.pages
                    record["replies_fetched"] = harvest.replies_fetched
                    return record
                except Exception as exc:
                    logger.warning("[youtube] innertube extract failed for %s: %s", video_id, exc)
                    continue

            elif backend == "yt_dlp":
                try:
                    cmd = [
                        "yt-dlp",
                        url,
                        "--dump-single-json",
                        "--skip-download",
                        "--get-comments",
                        "--extractor-args", f"youtube:max_comments={max_comments}",
                    ]
                    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
                    if proc.returncode == 0:
                        info = json.loads(proc.stdout)
                        raw_comments = info.get("comments", [])
                        root_map = {}
                        top_comments = []
                        replies_by_parent = {}

                        for rc in raw_comments:
                            cid = rc.get("id")
                            pid = rc.get("parent")
                            parsed_item = {
                                "id": cid,
                                "author": rc.get("author", "Anonymous"),
                                "text": clean_news_text(rc.get("text", "")),
                                "likes": rc.get("like_count", 0),
                                "timestamp": rc.get("timestamp"),
                                "reply_count": 0,
                                "replies": [],
                            }
                            if pid and pid != "root":
                                replies_by_parent.setdefault(pid, []).append(parsed_item)
                            else:
                                root_map[cid] = parsed_item
                                top_comments.append(parsed_item)

                        for pid, r_list in replies_by_parent.items():
                            if pid in root_map:
                                root_map[pid]["replies"] = r_list[:max_replies_per_comment]
                                root_map[pid]["reply_count"] = len(root_map[pid]["replies"])

                        parsed_comments = top_comments[:max_comments]

                        return build_video_record(
                            video_id,
                            url,
                            info.get("title", ""),
                            clean_news_text(info.get("description", "")),
                            parsed_comments,
                            "yt_dlp",
                        )
                except Exception as exc:
                    logger.warning("[youtube] yt-dlp extract failed for %s: %s", video_id, exc)
                    continue

            elif backend == "jina_reader":
                res = self.jina.read_markdown(url)
                if res.get("status") == "success":
                    raw_content = res.get("content", "")
                    return build_video_record(
                        video_id,
                        url,
                        res.get("title", ""),
                        clean_news_text(raw_content),
                        [],
                        "jina_reader",
                    )

        return {"url": url, "video_id": video_id, "status": "failed", "backend": "none", "content": ""}
