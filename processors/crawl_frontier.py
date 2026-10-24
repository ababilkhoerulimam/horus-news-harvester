"""
Resumable SQLite Crawl Frontier for Large-Scale Sports Scraping.
Tracks discovered candidate article URLs, crawl status (pending, in_progress, completed, failed),
and error states with WAL-mode concurrency for interrupted/crash-proof runs.
"""

import sqlite3
import threading
from collections import defaultdict, deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Set, Tuple


DEFAULT_FRONTIER_DB = "data/state/crawl_frontier.db"


class CrawlFrontier:
    """
    Persistent SQLite queue for large-scale multi-day sports harvesting.
    Guarantees zero work lost on network interruption, crash, or manual termination.
    Supports in-memory batch buffering to minimize SQLite disk lock contention.
    """

    def __init__(self, db_path: str = DEFAULT_FRONTIER_DB, batch_size: int = 50):
        self.db_path = Path(db_path)
        self.batch_size = batch_size
        self._lock = threading.Lock()
        self._completed_buffer: List[Tuple[str, str]] = []  # (now_iso, url)
        self._failed_buffer: List[Tuple[str, str, str]] = []  # (now_iso, error, url)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path), timeout=60.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA busy_timeout = 60000;")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crawl_frontier (
                    url TEXT PRIMARY KEY,
                    portal TEXT NOT NULL,
                    title TEXT,
                    published_date TEXT,
                    archive_date TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    discovered_at TEXT NOT NULL,
                    fetched_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_frontier_status ON crawl_frontier(status);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_frontier_portal ON crawl_frontier(portal);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_frontier_archive_date ON crawl_frontier(archive_date);")

    def add_candidates(self, items: List[Dict[str, str]], portal: str) -> int:
        """
        Inserts new discovered candidates using INSERT OR IGNORE.
        Returns count of newly added URLs.
        """
        if not items:
            return 0

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        for item in items:
            u = item.get("url", "").strip()
            if not u:
                continue
            rows.append((
                u,
                portal,
                item.get("title", ""),
                item.get("published_date", ""),
                item.get("archive_date", ""),
                "pending",
                now_iso,
                0,
            ))

        if not rows:
            return 0

        with self._connection() as conn:
            cursor = conn.executemany("""
                INSERT OR IGNORE INTO crawl_frontier (
                    url, portal, title, published_date, archive_date,
                    status, discovered_at, retry_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
            """, rows)
            return cursor.rowcount

    def get_pending_batch(self, limit: int = 100, portal: Optional[str] = None) -> List[Dict[str, Any]]:
        """Retrieves a batch of pending or retryable failed URLs (auto-flushes in-memory buffer first)."""
        self.flush()
        query = """
            SELECT url, portal, title, published_date, archive_date, retry_count
            FROM crawl_frontier
            WHERE status = 'pending' OR (status = 'failed' AND retry_count < 3)
        """
        params: List[Any] = []
        if portal:
            query += " AND portal = ?"
            params.append(portal)

        query += " ORDER BY retry_count ASC, discovered_at ASC LIMIT ?"
        params.append(limit)

        with self._connection() as conn:
            cur = conn.execute(query, params)
            return [dict(r) for r in cur.fetchall()]

    def claim_pending_batch(self, limit: int = 50, portal: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Atomically retrieves pending URLs and marks them 'in_progress' in a single transaction.
        Prevents race conditions between concurrent 2-core worker processes.
        """
        self.flush()
        query = """
            SELECT url, portal, title, published_date, archive_date, retry_count
            FROM crawl_frontier
            WHERE status = 'pending' OR (status = 'failed' AND retry_count < 3)
        """
        params: List[Any] = []
        if portal:
            query += " AND portal = ?"
            params.append(portal)

        query += " ORDER BY retry_count ASC, discovered_at ASC LIMIT ?"
        params.append(limit)

        with self._connection() as conn:
            cur = conn.execute(query, params)
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                urls = [r["url"] for r in rows]
                chunk_size = 500
                for i in range(0, len(urls), chunk_size):
                    chunk = urls[i:i + chunk_size]
                    placeholders = ",".join(["?"] * len(chunk))
                    conn.execute(f"""
                        UPDATE crawl_frontier
                        SET status = 'in_progress'
                        WHERE url IN ({placeholders});
                    """, chunk)
            return rows

    def mark_in_progress(self, urls: List[str]) -> None:
        """Marks batch as in_progress in SQLite-safe parameter chunks."""
        if not urls:
            return
        chunk_size = 500
        with self._connection() as conn:
            for i in range(0, len(urls), chunk_size):
                chunk = urls[i:i + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                conn.execute(f"""
                    UPDATE crawl_frontier
                    SET status = 'in_progress'
                    WHERE url IN ({placeholders}) AND status = 'pending';
                """, chunk)

    def mark_completed(self, url: str, immediate: bool = False) -> None:
        """Buffers article completion and flushes when reaching batch_size or immediate=True."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._completed_buffer.append((now_iso, url))
            if immediate or len(self._completed_buffer) >= self.batch_size:
                self._flush_completed_locked()

    def mark_failed(self, url: str, error: str = "", immediate: bool = False) -> None:
        """Buffers article failure and flushes when reaching batch_size or immediate=True."""
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with self._lock:
            self._failed_buffer.append((now_iso, str(error)[:500], url))
            if immediate or len(self._failed_buffer) >= self.batch_size:
                self._flush_failed_locked()

    def flush(self) -> None:
        """Flushes buffered completed and failed items to SQLite in one transaction."""
        with self._lock:
            self._flush_completed_locked()
            self._flush_failed_locked()

    def _flush_completed_locked(self) -> None:
        if not self._completed_buffer:
            return
        to_write = self._completed_buffer[:]
        self._completed_buffer.clear()
        try:
            with self._connection() as conn:
                conn.executemany("""
                    UPDATE crawl_frontier
                    SET status = 'completed', fetched_at = ?, error_message = NULL
                    WHERE url = ?;
                """, to_write)
        except Exception:
            self._completed_buffer.extend(to_write)
            raise

    def _flush_failed_locked(self) -> None:
        if not self._failed_buffer:
            return
        to_write = self._failed_buffer[:]
        self._failed_buffer.clear()
        try:
            with self._connection() as conn:
                conn.executemany("""
                    UPDATE crawl_frontier
                    SET status = 'failed',
                        retry_count = retry_count + 1,
                        fetched_at = ?,
                        error_message = ?
                    WHERE url = ?;
                """, to_write)
        except Exception:
            self._failed_buffer.extend(to_write)
            raise

    def reset_in_progress(self) -> int:
        """Resets stuck 'in_progress' items back to 'pending' on startup/resume."""
        self.flush()
        with self._connection() as conn:
            cur = conn.execute("""
                UPDATE crawl_frontier
                SET status = 'pending'
                WHERE status = 'in_progress';
            """)
            return cur.rowcount

    def get_frontier_summary(self) -> Dict[str, Any]:
        """Provides status breakdown per portal and aggregate totals (auto-flushes buffer first)."""
        self.flush()
        with self._connection() as conn:
            cur = conn.execute("""
                SELECT portal, status, COUNT(*) as cnt
                FROM crawl_frontier
                GROUP BY portal, status;
            """)
            breakdown: Dict[str, Dict[str, int]] = {}
            totals: Dict[str, int] = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0, "total": 0}

            for row in cur.fetchall():
                p = row["portal"]
                st = row["status"]
                cnt = row["cnt"]
                if p not in breakdown:
                    breakdown[p] = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0, "total": 0}
                breakdown[p][st] = cnt
                breakdown[p]["total"] += cnt
                if st in totals:
                    totals[st] += cnt
                totals["total"] += cnt

            return {
                "totals": totals,
                "portals": breakdown,
            }

    def close(self) -> None:
        """Flushes and closes frontier database session."""
        self.flush()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def interleave_by_portal(items: List[Dict[str, Any]], key: str = "portal") -> List[Dict[str, Any]]:
    """
    Round-robin interleaves candidate items by portal/domain.
    Spreads concurrent traffic evenly across target hosts, avoiding single-origin bottleneck.
    O(N) runtime and O(N) memory.
    """
    if not items:
        return []

    groups: Dict[str, deque] = defaultdict(deque)
    for item in items:
        portal_val = item.get(key) or "unknown"
        groups[portal_val].append(item)

    interleaved: List[Dict[str, Any]] = []
    active_keys = list(groups.keys())
    while active_keys:
        next_active = []
        for p in active_keys:
            d = groups[p]
            if d:
                interleaved.append(d.popleft())
            if d:
                next_active.append(p)
        active_keys = next_active

    return interleaved
