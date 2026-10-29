"""Canonical harvest record shapes and the JSONL sink shared by all workers."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("record_writer")

PROGRESS_EVERY = 50

# Below this many characters an article body is a fragment, not an article.
# Chosen from the 225k-record corpus, not guessed: fragment-heavy domains top out
# at p75 = 596 (detik), 969 (kompas), 376 (inews), while healthy portals start at
# p10 = 1694 (niaga) and 4293 (okezone). 1000 sits in that empty gap, so it flags
# the broken domains (detik 90%, kompas 76%, inews 98%) and almost no healthy one
# (okezone 9 of 34,430 records).
MIN_QUALITY_CHARS = 1000


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def quality_of(content_chars: int, min_quality_chars: int = MIN_QUALITY_CHARS) -> str:
    """Labels a body as "ok" or "low" so downstream can filter without re-reading it."""
    return "low" if content_chars < min_quality_chars else "ok"


def build_news_record(
    url: str,
    title: Optional[str],
    published_date: Optional[str],
    archive_date: Optional[str],
    content: str,
    backend: str,
    quality: Optional[str] = None,
    content_chars: int = 0,
) -> Dict[str, Any]:
    """Record shape for the historical news backfill output."""
    chars = content_chars or len(content)
    return {
        "url": url,
        "title": title or "",
        "published_date": published_date or "",
        "archive_date": archive_date or "",
        "content": content,
        "source": urlparse(url).netloc,
        "backend": backend,
        "quality": quality or quality_of(chars),
        "content_chars": chars,
        "harvested_at": _utc_stamp(),
    }


def build_topic_record(
    url: str,
    title: Optional[str],
    content: str,
    topic_id: str,
    portal: str,
    publish_date: Optional[str],
    quality: Optional[str] = None,
    content_chars: int = 0,
) -> Dict[str, Any]:
    """Record shape for the topic harvester output."""
    chars = content_chars or len(content)
    return {
        "url": url,
        "title": title or "",
        "content": content,
        "topic": topic_id,
        "portal": portal,
        "publish_date": publish_date or "",
        "quality": quality or quality_of(chars),
        "content_chars": chars,
        "harvested_at": datetime.now(timezone.utc).isoformat(),
    }


class JsonlSink:
    """Append-only JSONL writer that keeps the frontier in step with the file."""

    def __init__(
        self,
        handle,
        frontier,
        label: str,
        output_name: str,
        total: Optional[int] = None,
        progress_every: int = PROGRESS_EVERY,
    ):
        self._handle = handle
        self._frontier = frontier
        self._label = label
        self._output_name = output_name
        self._total = total
        self._progress_every = progress_every
        self._file_lock = asyncio.Lock()
        self._db_lock = asyncio.Lock()
        self.count = 0

    async def emit(self, record: Dict[str, Any]) -> None:
        url = record["url"]
        async with self._file_lock:
            self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            self.count += 1
            written = self.count
        async with self._db_lock:
            self._frontier.mark_completed(url)
        if written % self._progress_every == 0:
            await self.flush()
            logger.info("%s Harvested %s%s articles -> %s", self._label, written, self._progress_suffix(), self._output_name)

    async def fail(self, url: str, reason: str) -> None:
        async with self._db_lock:
            self._frontier.mark_failed(url, error=reason)

    async def flush(self) -> None:
        async with self._file_lock:
            self._handle.flush()
        async with self._db_lock:
            self._frontier.flush()

    def _progress_suffix(self) -> str:
        return "" if self._total is None else "/" + str(self._total)
