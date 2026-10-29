"""Single mapping from YouTube payloads to video and comment records."""

from typing import Any, Dict, List, Optional

from processors.cleaner import clean_news_text, normalize_timestamp
from processors.metrics import parse_count


def build_comment(
    comment_id: str,
    author: Optional[str],
    text: str,
    likes: Any,
    published: Optional[str],
    parent_id: Optional[str] = None,
    depth: int = 0,
    reply_count: int = 0,
) -> Dict[str, Any]:
    """Builds one comment record with numeric likes and an ISO timestamp."""
    return {
        "id": comment_id,
        "parent_id": parent_id,
        "depth": depth,
        "author": author or "Anonymous",
        "text": clean_news_text(text or ""),
        "likes": parse_count(likes),
        "timestamp": normalize_timestamp(published),
        "reply_count": reply_count,
        "replies": [],
    }


def build_video_record(
    video_id: str,
    url: str,
    title: str,
    content: str,
    comments: List[Dict[str, Any]],
    backend: str,
) -> Dict[str, Any]:
    """Builds the video record that carries its harvested comments."""
    return {
        "url": url,
        "video_id": video_id,
        "title": title or "",
        "content": content or "",
        "comment_count": len(comments),
        "comments": comments,
        "backend": backend,
        "status": "success",
    }
