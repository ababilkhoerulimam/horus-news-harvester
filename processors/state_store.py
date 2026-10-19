"""
SQLite State & Cursor Persistence Store.
Tracks harvested article URLs, YouTube video IDs, and Twitter tweet IDs.
Guarantees delta-only incremental runs without re-scraping or memory bloat.
"""

import hashlib
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional


class StateStore:
    """Persistent SQLite tracking store for deduplication and incremental cursors."""

    def __init__(self, db_path: str = "data/state/harvest_state.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_items (
                    item_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    url_or_id TEXT NOT NULL,
                    content_hash TEXT,
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_channel_sport ON seen_items(channel, sport);")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS harvest_cursors (
                    channel TEXT NOT NULL,
                    sport TEXT NOT NULL,
                    target_key TEXT NOT NULL,
                    cursor_val TEXT,
                    last_harvest_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (channel, sport, target_key)
                );
            """)

    @staticmethod
    def _compute_hash(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]

    def is_seen(self, item_id: str) -> bool:
        with self._get_connection() as conn:
            cur = conn.execute("SELECT 1 FROM seen_items WHERE item_id = ? LIMIT 1;", (str(item_id),))
            return cur.fetchone() is not None

    def filter_unseen(
        self,
        channel: str,
        items: List[Dict[str, Any]],
        id_key: str = "url"
    ) -> List[Dict[str, Any]]:
        if not items:
            return []

        item_map = {}
        for it in items:
            raw_id = it.get(id_key) or it.get("url") or it.get("tweet_id") or it.get("title", "")
            if raw_id:
                item_map[str(raw_id)] = it

        if not item_map:
            return []

        keys = list(item_map.keys())
        # ponytail: SQLite query chunks limited to 500 params; expand if batch sizes exceed thousands.
        chunk_size = 500
        seen_set = set()

        with self._get_connection() as conn:
            for i in range(0, len(keys), chunk_size):
                chunk = keys[i:i + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                query = f"SELECT item_id FROM seen_items WHERE item_id IN ({placeholders});"
                cur = conn.execute(query, chunk)
                for row in cur.fetchall():
                    seen_set.add(row[0])

        return [it for it in items if str(it.get(id_key) or it.get("url") or it.get("tweet_id") or it.get("title", "")) not in seen_set]

    def mark_seen(
        self,
        channel: str,
        sport: str,
        items: List[Dict[str, Any]],
        id_key: str = "url"
    ) -> int:
        if not items:
            return 0

        rows = []
        for it in items:
            raw_id = it.get(id_key) or it.get("url") or it.get("tweet_id") or it.get("title", "")
            if not raw_id:
                continue
            item_id = str(raw_id)
            url_or_id = it.get("url") or item_id
            content = it.get("content") or it.get("text") or it.get("title", "")
            chash = self._compute_hash(content)
            rows.append((item_id, channel, sport, url_or_id, chash))

        if not rows:
            return 0

        with self._get_connection() as conn:
            conn.executemany("""
                INSERT OR IGNORE INTO seen_items (item_id, channel, sport, url_or_id, content_hash)
                VALUES (?, ?, ?, ?, ?);
            """, rows)

        return len(rows)

    def get_cursor(self, channel: str, sport: str, target_key: str) -> Optional[str]:
        with self._get_connection() as conn:
            cur = conn.execute("""
                SELECT cursor_val FROM harvest_cursors
                WHERE channel = ? AND sport = ? AND target_key = ?;
            """, (channel, sport, target_key))
            row = cur.fetchone()
            return row[0] if row else None

    def set_cursor(self, channel: str, sport: str, target_key: str, cursor_val: str) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                INSERT INTO harvest_cursors (channel, sport, target_key, cursor_val, last_harvest_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(channel, sport, target_key) DO UPDATE SET
                    cursor_val = excluded.cursor_val,
                    last_harvest_at = CURRENT_TIMESTAMP;
            """, (channel, sport, target_key, str(cursor_val)))

    def get_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            total_items = conn.execute("SELECT COUNT(*) FROM seen_items;").fetchone()[0]
            by_channel = {}
            for row in conn.execute("SELECT channel, COUNT(*) FROM seen_items GROUP BY channel;"):
                by_channel[row[0]] = row[1]
            return {
                "total_items": total_items,
                "by_channel": by_channel,
                "db_path": str(self.db_path)
            }
