"""
YouTube Sports Channel & Comment Harvester.
Implements multi-backend fallback: InnerTube API -> yt-dlp -> Jina Reader.
Extracts video metadata and fan comments for sentiment & discourse analysis.
"""

import json
import re
import subprocess
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from .base import SportsChannel
from backends.jina_reader import JinaReaderBackend
from processors.cleaner import clean_news_text
from processors.http_client import default_http_client


INNERTUBE_URL_SEARCH = "https://www.youtube.com/youtubei/v1/search"
INNERTUBE_URL_NEXT = "https://www.youtube.com/youtubei/v1/next"
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
        except Exception:
            pass

        self.active_backend = "innertube_api"
        status_msg = f"Active: innertube_api, yt-dlp: {'available' if yt_dlp_ok else 'not found'}"
        return "ok", status_msg

    def _extract_video_id(self, url: str) -> Optional[str]:
        match = re.search(r"(?:v=|\/embed\/|\/watch\?v=|youtu\.be\/|\/v\/)([a-zA-Z0-9_-]{11})", url)
        return match.group(1) if match else None

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
                    data = json.dumps(payload).encode("utf-8")

                    status, body, _ = default_http_client.fetch(
                        INNERTUBE_URL_SEARCH,
                        data=data,
                        headers={"Content-Type": "application/json"},
                        timeout=12,
                        use_jitter=False,
                    )
                    if status != 200 or not body:
                        continue
                    res_json = json.loads(body.decode("utf-8"))

                    # Traverse contents
                    contents = (
                        res_json.get("contents", {})
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
                                return results

                    if results:
                        return results
                except Exception:
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
                except Exception:
                    continue

        return results

    def _parse_innertube_comments_page(
        self,
        page_json: Dict[str, Any],
        max_comments: int,
        seen_ids: set
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """
        Parses viewer comments and next continuation token from an InnerTube next response.
        Supports both modern entity updates (commentEntityPayload) and legacy commentThreadRenderer.
        Extracts nested reply continuation tokens when available.
        """
        comments: List[Dict[str, Any]] = []

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

            comments.append({
                "id": cid,
                "author": author,
                "text": clean_news_text(content_text),
                "likes": likes,
                "timestamp": published,
                "reply_count": 0,
                "replies": [],
            })
            if len(comments) >= max_comments:
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

                    # Extract reply continuation token if sub-thread exists
                    reply_token = None
                    replies_renderer = ctr.get("replies", {}).get("commentRepliesRenderer", {})
                    for r_item in replies_renderer.get("contents", []):
                        cir = r_item.get("continuationItemRenderer")
                        if cir:
                            reply_token = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                            if reply_token:
                                break
                    if not reply_token:
                        view_cmd = replies_renderer.get("viewReplies", {}).get("buttonRenderer", {}).get("command", {})
                        reply_token = view_cmd.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")

                    c_entry = {
                        "id": cid,
                        "author": author,
                        "text": clean_news_text(text),
                        "likes": likes,
                        "timestamp": published,
                        "reply_count": 0,
                        "replies": [],
                    }
                    if reply_token:
                        c_entry["_reply_token"] = reply_token

                    comments.append(c_entry)
                    if len(comments) >= max_comments:
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
                cir = item.get("continuationItemRenderer")
                if cir:
                    token = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                    if token:
                        next_token = token
                        break
            if next_token:
                break

        return comments, next_token

    def _fetch_innertube_replies(
        self,
        reply_token: Optional[str],
        max_replies: int = 20,
        max_pages: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetches nested replies for a root comment via InnerTube continuation token.
        Dynamically paginates through continuation tokens up to max_replies.
        """
        # ponytail: Dynamic bounded reply fetching; scales max_pages with requested max_replies.
        if not reply_token or max_replies <= 0:
            return []

        if max_pages is None:
            max_pages = max(2, min(50, (max_replies + 9) // 10))

        replies: List[Dict[str, Any]] = []
        token = reply_token
        page = 0
        seen_reply_ids = set()

        while token and len(replies) < max_replies and page < max_pages:
            page += 1
            try:
                payload = dict(INNERTUBE_CLIENT)
                payload["continuation"] = token
                data = json.dumps(payload).encode("utf-8")

                status, body, _ = default_http_client.fetch(
                    INNERTUBE_URL_NEXT,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                    use_jitter=False,
                )
                if status != 200 or not body:
                    break
                page_json = json.loads(body.decode("utf-8"))

                next_token = None
                endpoints = page_json.get("onResponseReceivedEndpoints", [])
                for ep in endpoints:
                    cmd = (
                        ep.get("appendContinuationItemsAction")
                        or ep.get("appendContinuationItemsCommand")
                        or ep.get("reloadContinuationItemsCommand")
                    )
                    if not cmd:
                        continue
                    for item in cmd.get("continuationItems", []):
                        cr = item.get("commentRenderer")
                        if cr:
                            rid = cr.get("commentId")
                            if rid and rid not in seen_reply_ids:
                                seen_reply_ids.add(rid)
                                text_runs = cr.get("contentText", {}).get("runs", [])
                                text = "".join(r.get("text", "") for r in text_runs)
                                author = (
                                    cr.get("authorText", {}).get("simpleText")
                                    or "".join(r.get("text", "") for r in cr.get("authorText", {}).get("runs", []))
                                    or "Anonymous"
                                )
                                likes = cr.get("voteCount", {}).get("simpleText", "0")
                                published = cr.get("publishedTimeText", {}).get("simpleText", "")

                                replies.append({
                                    "id": rid,
                                    "author": author,
                                    "text": clean_news_text(text),
                                    "likes": likes,
                                    "timestamp": published,
                                })
                                if len(replies) >= max_replies:
                                    break

                        cir = item.get("continuationItemRenderer")
                        if cir:
                            t = cir.get("continuationEndpoint", {}).get("continuationCommand", {}).get("token")
                            if t:
                                next_token = t

                token = next_token if len(replies) < max_replies else None
            except Exception:
                break

        return replies

    def _fetch_innertube_comments(
        self,
        init_token: Optional[str],
        max_comments: int = 50,
        max_replies_per_comment: int = 10,
        fetch_replies: bool = True
    ) -> List[Dict[str, Any]]:
        """
        Loops through InnerTube continuation tokens to harvest deep video comments.
        Capped to max_pages to prevent excessive network requests on videos with 10k+ comments.
        """
        if not init_token or max_comments <= 0:
            return []

        collected_comments: List[Dict[str, Any]] = []
        seen_ids = set()
        token: Optional[str] = init_token
        page = 0
        # ponytail: Capped at max 250 pages (up to 5,000 comments per video); adjust max_comments if needing deeper discourse mining.
        max_pages = min(250, max(1, (max_comments + 19) // 20))

        while token and len(collected_comments) < max_comments and page < max_pages:
            page += 1
            try:
                payload = dict(INNERTUBE_CLIENT)
                payload["continuation"] = token
                data = json.dumps(payload).encode("utf-8")

                status, body, _ = default_http_client.fetch(
                    INNERTUBE_URL_NEXT,
                    data=data,
                    headers={"Content-Type": "application/json"},
                    timeout=12,
                    use_jitter=True,
                )
                if status != 200 or not body:
                    break
                page_json = json.loads(body.decode("utf-8"))

                remaining = max_comments - len(collected_comments)
                page_comments, next_token = self._parse_innertube_comments_page(page_json, remaining, seen_ids)

                for c in page_comments:
                    reply_tok = c.pop("_reply_token", None)
                    if fetch_replies and reply_tok:
                        c["replies"] = self._fetch_innertube_replies(reply_tok, max_replies=max_replies_per_comment)
                        c["reply_count"] = len(c["replies"])

                collected_comments.extend(page_comments)
                token = next_token
            except Exception:
                break

        return collected_comments

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extracts video details and top viewer comments with nested replies."""
        backends_to_try = self.ordered_backends(config)
        video_id = self._extract_video_id(url)
        max_comments = config.get("max_comments", 50) if config else 50
        max_replies_per_comment = config.get("max_replies_per_comment", 10) if config else 10

        if not video_id:
            return {"url": url, "status": "failed", "error": "Invalid YouTube video URL", "backend": "none"}

        for backend in backends_to_try:
            if backend == "innertube_api":
                try:
                    payload = dict(INNERTUBE_CLIENT)
                    payload["videoId"] = video_id
                    data = json.dumps(payload).encode("utf-8")

                    status, body, _ = default_http_client.fetch(
                        INNERTUBE_URL_NEXT,
                        data=data,
                        headers={"Content-Type": "application/json"},
                        timeout=12,
                        use_jitter=False,
                    )
                    if status != 200 or not body:
                        continue
                    res_json = json.loads(body.decode("utf-8"))

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
                    except Exception:
                        pass

                    comments = self._fetch_innertube_comments(
                        init_token,
                        max_comments=max_comments,
                        max_replies_per_comment=max_replies_per_comment
                    )
                    return {
                        "url": url,
                        "video_id": video_id,
                        "title": title,
                        "content": clean_news_text(description or title),
                        "comment_count": len(comments),
                        "comments": comments,
                        "backend": "innertube_api",
                        "status": "success",
                    }
                except Exception:
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

                        return {
                            "url": url,
                            "video_id": video_id,
                            "title": info.get("title", ""),
                            "content": clean_news_text(info.get("description", "")),
                            "comment_count": len(parsed_comments),
                            "comments": parsed_comments,
                            "backend": "yt_dlp",
                            "status": "success",
                        }
                except Exception:
                    continue

            elif backend == "jina_reader":
                res = self.jina.read_markdown(url)
                if res.get("status") == "success":
                    raw_content = res.get("content", "")
                    return {
                        "url": url,
                        "video_id": video_id,
                        "title": res.get("title", ""),
                        "content": clean_news_text(raw_content),
                        "comment_count": 0,
                        "comments": [],
                        "backend": "jina_reader",
                        "status": "success",
                    }

        return {"url": url, "video_id": video_id, "status": "failed", "backend": "none", "content": ""}
