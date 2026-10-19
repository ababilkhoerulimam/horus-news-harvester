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

                    req = urllib.request.Request(
                        INNERTUBE_URL_SEARCH,
                        data=data,
                        headers={
                            "User-Agent": WEB_USER_AGENT,
                            "Content-Type": "application/json",
                        }
                    )
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        res_json = json.loads(resp.read().decode("utf-8"))

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

    def extract(self, url: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Extracts video details and top viewer comments."""
        backends_to_try = self.ordered_backends(config)
        video_id = self._extract_video_id(url)
        max_comments = config.get("max_comments", 50) if config else 50

        if not video_id:
            return {"url": url, "status": "failed", "error": "Invalid YouTube video URL", "backend": "none"}

        for backend in backends_to_try:
            if backend == "innertube_api":
                try:
                    payload = dict(INNERTUBE_CLIENT)
                    payload["videoId"] = video_id
                    data = json.dumps(payload).encode("utf-8")

                    req = urllib.request.Request(
                        INNERTUBE_URL_NEXT,
                        data=data,
                        headers={
                            "User-Agent": WEB_USER_AGENT,
                            "Content-Type": "application/json",
                        }
                    )
                    with urllib.request.urlopen(req, timeout=12) as resp:
                        res_json = json.loads(resp.read().decode("utf-8"))

                    # Extract video title and description
                    title = ""
                    description = ""
                    try:
                        results_cont = res_json.get("contents", {}).get("twoColumnWatchNextResults", {}).get("results", {}).get("results", {}).get("contents", [])
                        for c in results_cont:
                            item = c.get("videoPrimaryInfoRenderer")
                            if item:
                                title = "".join(r.get("text", "") for r in item.get("title", {}).get("runs", []))
                            sec = c.get("videoSecondaryInfoRenderer")
                            if sec:
                                description = "".join(r.get("text", "") for r in sec.get("attributedDescription", {}).get("content", ""))
                    except Exception:
                        pass

                    comments: List[Dict[str, Any]] = []
                    # Parse initial comment section if present
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
                        parsed_comments = []
                        for rc in raw_comments[:max_comments]:
                            parsed_comments.append({
                                "id": rc.get("id"),
                                "author": rc.get("author", "Anonymous"),
                                "text": clean_news_text(rc.get("text", "")),
                                "likes": rc.get("like_count", 0),
                                "timestamp": rc.get("timestamp"),
                            })

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
