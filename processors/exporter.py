"""
Analytical Exporter for Indonesian Sports Harvester.
Transforms raw JSONL files into high-performance indexed SQLite, DuckDB, or Parquet datasets.
Optimized for analytical queries, aggregations, and discourse mining without full linear scans.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from processors.cleaner import normalize_timestamp
from processors.metrics import parse_count
from processors.sqlite_util import connect_wal


# Longest wins. A record can be written more than once -- a re-harvest, or the same
# URL under two sport files -- and INSERT OR REPLACE would let whichever file sorts
# last overwrite a good body with a 400-character stub.
INSERT_ARTICLES = """
    INSERT INTO articles
    (url, title, published_date, summary, author, source, backend, sport, content, extracted_via)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(url) DO UPDATE SET
        title = excluded.title,
        published_date = excluded.published_date,
        summary = excluded.summary,
        author = excluded.author,
        source = excluded.source,
        backend = excluded.backend,
        sport = excluded.sport,
        content = excluded.content,
        extracted_via = excluded.extracted_via
    WHERE length(excluded.content) > length(articles.content)
"""

INSERT_YOUTUBE_VIDEOS = """
    INSERT OR REPLACE INTO youtube_videos
    (video_id, title, url, author, published_date, sport, source, backend, comment_count, description)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_YOUTUBE_COMMENTS = """
    INSERT OR REPLACE INTO youtube_comments
    (comment_id, video_id, parent_id, author, text, likes, timestamp, reply_count, is_reply)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_TWEETS = """
    INSERT OR REPLACE INTO tweets
    (tweet_id, username, author, title, text, url, published_date, likes, retweets, replies, backend, sport)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _flush(conn, sql, rows, stats, key):
    if not rows:
        return
    conn.executemany(sql, rows)
    stats[key] += len(rows)
    rows.clear()

class SportsAnalyticsExporter:
    """Streams raw JSONL data into optimized relational/columnar analytical stores."""

    def __init__(self, raw_data_dir: str = "data/raw", analytics_dir: str = "data/analytics"):
        self.raw_data_dir = Path(raw_data_dir)
        self.analytics_dir = Path(analytics_dir)
        self.analytics_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _stream_jsonl(file_path: Path) -> Generator[Dict[str, Any], None, None]:
        """Streams records line-by-line to prevent high memory footprint on large datasets."""
        if not file_path.exists():
            return
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield json.loads(stripped)
                except Exception:
                    continue

    # Target 1: SQLite Analytical Database (Zero-dependency, WAL, B-Tree Indexes)

    @contextmanager
    def _sqlite_conn(self, db_path: Path) -> Generator[sqlite3.Connection, None, None]:
        # ponytail: PRAGMAs tuned for analytical read speed and fast bulk ingestion.
        with connect_wal(
            db_path,
            extra_pragmas=(
                "PRAGMA cache_size = -64000;",
                "PRAGMA temp_store = MEMORY;",
                "PRAGMA mmap_size = 268435456;",
            ),
        ) as conn:
            yield conn

    def _init_sqlite_schema(self, conn: sqlite3.Connection) -> None:
        """Initializes tables and indexes for multi-channel sports data."""
        # 1. News Articles Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS articles (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                published_date TEXT,
                summary TEXT,
                author TEXT,
                source TEXT,
                backend TEXT,
                sport TEXT NOT NULL,
                content TEXT,
                extracted_via TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_sport ON articles(sport);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);")

        # 2. YouTube Videos Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_videos (
                video_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                url TEXT,
                author TEXT,
                published_date TEXT,
                sport TEXT NOT NULL,
                source TEXT,
                backend TEXT,
                comment_count INTEGER DEFAULT 0,
                description TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yt_sport ON youtube_videos(sport);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yt_author ON youtube_videos(author);")

        # 3. YouTube Comments & Replies Table (Flattened for relational queries)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS youtube_comments (
                comment_id TEXT PRIMARY KEY,
                video_id TEXT NOT NULL,
                parent_id TEXT,
                author TEXT,
                text TEXT,
                likes INTEGER DEFAULT 0,
                timestamp TEXT,
                reply_count INTEGER DEFAULT 0,
                is_reply INTEGER DEFAULT 0,
                FOREIGN KEY (video_id) REFERENCES youtube_videos(video_id)
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yt_comm_vid ON youtube_comments(video_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yt_comm_pid ON youtube_comments(parent_id);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_yt_comm_author ON youtube_comments(author);")

        # 4. Twitter/X Discussions Table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tweets (
                tweet_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                author TEXT,
                title TEXT,
                text TEXT,
                url TEXT,
                published_date TEXT,
                likes INTEGER DEFAULT 0,
                retweets INTEGER DEFAULT 0,
                replies INTEGER DEFAULT 0,
                backend TEXT,
                sport TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tweets_sport ON tweets(sport);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tweets_user ON tweets(username);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tweets_pub ON tweets(published_date);")

    def export_sqlite(
        self,
        db_path: Optional[str] = None,
        channels: Optional[List[str]] = None,
        batch_size: int = 2000
    ) -> Dict[str, int]:
        """
        Exports all discovered JSONL files to a normalized SQLite analytical database.
        Returns record counts per table.
        """
        target_path = Path(db_path) if db_path else self.analytics_dir / "sports_analytics.db"
        target_path.parent.mkdir(parents=True, exist_ok=True)

        selected_channels = set(channels or ["news", "youtube", "twitter"])
        stats = {
            "articles": 0,
            "youtube_videos": 0,
            "youtube_comments": 0,
            "tweets": 0,
        }

        with self._sqlite_conn(target_path) as conn:
            self._init_sqlite_schema(conn)

            # 1. Export News Articles
            if "news" in selected_channels:
                news_folder = self.raw_data_dir / "news"
                if news_folder.exists():
                    batch = []
                    for f in sorted(news_folder.glob("*.jsonl")):
                        sport_name = f.stem
                        for item in self._stream_jsonl(f):
                            url = item.get("url")
                            if not url:
                                continue
                            batch.append((
                                url,
                                item.get("title", ""),
                                normalize_timestamp(item.get("published_date", "")),
                                item.get("summary", ""),
                                item.get("author", ""),
                                item.get("source", ""),
                                item.get("backend", ""),
                                item.get("sport", sport_name),
                                item.get("content", ""),
                                item.get("extracted_via", ""),
                            ))
                            if len(batch) >= batch_size:
                                _flush(conn, INSERT_ARTICLES, batch, stats, "articles")
                    if batch:
                        _flush(conn, INSERT_ARTICLES, batch, stats, "articles")

            # 2. Export YouTube Videos & Comments
            if "youtube" in selected_channels:
                yt_folder = self.raw_data_dir / "youtube"
                if yt_folder.exists():
                    v_batch = []
                    c_batch = []
                    for f in sorted(yt_folder.glob("*.jsonl")):
                        sport_name = f.stem
                        for item in self._stream_jsonl(f):
                            vid = item.get("video_id") or item.get("id")
                            if not vid:
                                continue
                            v_batch.append((
                                vid,
                                item.get("title", ""),
                                item.get("url", ""),
                                item.get("author", ""),
                                normalize_timestamp(item.get("published_date", "")),
                                item.get("sport", sport_name),
                                item.get("source", "youtube"),
                                item.get("backend", ""),
                                item.get("comment_count", 0),
                                item.get("description", ""),
                            ))

                            # Extract root comments and nested replies
                            for c in item.get("comments", []):
                                cid = c.get("id")
                                if not cid:
                                    continue
                                c_batch.append((
                                    cid,
                                    vid,
                                    None,
                                    c.get("author", "Anonymous"),
                                    c.get("text", ""),
                                    parse_count(c.get("likes", 0)),
                                    str(c.get("timestamp", "")),
                                    len(c.get("replies", [])),
                                    0,
                                ))

                                for r in c.get("replies", []):
                                    rid = r.get("id")
                                    if not rid:
                                        continue
                                    c_batch.append((
                                        rid,
                                        vid,
                                        cid,
                                        r.get("author", "Anonymous"),
                                        r.get("text", ""),
                                        parse_count(r.get("likes", 0)),
                                        str(r.get("timestamp", "")),
                                        0,
                                        1,
                                    ))

                            if len(v_batch) >= batch_size:
                                _flush(conn, INSERT_YOUTUBE_VIDEOS, v_batch, stats, "youtube_videos")

                            if len(c_batch) >= batch_size:
                                _flush(conn, INSERT_YOUTUBE_COMMENTS, c_batch, stats, "youtube_comments")

                    if v_batch:
                        _flush(conn, INSERT_YOUTUBE_VIDEOS, v_batch, stats, "youtube_videos")

                    if c_batch:
                        _flush(conn, INSERT_YOUTUBE_COMMENTS, c_batch, stats, "youtube_comments")

            # 3. Export Tweets
            if "twitter" in selected_channels:
                tw_folder = self.raw_data_dir / "twitter"
                if tw_folder.exists():
                    t_batch = []
                    for f in sorted(tw_folder.glob("*.jsonl")):
                        sport_name = f.stem
                        for item in self._stream_jsonl(f):
                            tid = item.get("tweet_id")
                            if not tid:
                                continue
                            metrics = item.get("metrics", {}) or {}
                            t_batch.append((
                                str(tid),
                                item.get("username", ""),
                                item.get("author", ""),
                                item.get("title", ""),
                                item.get("text", ""),
                                item.get("url", ""),
                                normalize_timestamp(item.get("published_date", "")),
                                parse_count(metrics.get("likes", 0)),
                                parse_count(metrics.get("retweets", 0)),
                                parse_count(metrics.get("replies", 0)),
                                item.get("backend", ""),
                                item.get("sport", sport_name),
                            ))
                            if len(t_batch) >= batch_size:
                                _flush(conn, INSERT_TWEETS, t_batch, stats, "tweets")

                    if t_batch:
                        _flush(conn, INSERT_TWEETS, t_batch, stats, "tweets")

        return stats

    # Target 2: DuckDB Analytical Database (Columnar, ZSTD)

    def export_duckdb(
        self,
        db_path: Optional[str] = None,
        channels: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Exports data into a native DuckDB columnar file.
        Leverages SQLite export first, then imports directly into DuckDB tables for zero schema friction.
        """
        try:
            import duckdb
        except ImportError:
            return {"status": "error", "message": "duckdb package is not installed"}

        temp_sqlite = self.analytics_dir / "_temp_export.db"
        sqlite_stats = self.export_sqlite(db_path=str(temp_sqlite), channels=channels)

        target_path = Path(db_path) if db_path else self.analytics_dir / "sports_analytics.duckdb"
        target_path.parent.mkdir(parents=True, exist_ok=True)

        dconn = duckdb.connect(str(target_path))
        try:
            dconn.execute("INSTALL sqlite; LOAD sqlite;")
            dconn.execute(f"ATTACH '{temp_sqlite}' AS raw_sqlite (TYPE SQLITE);")

            for table in ["articles", "youtube_videos", "youtube_comments", "tweets"]:
                dconn.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM raw_sqlite.{table};")

            dconn.execute("DETACH raw_sqlite;")
        finally:
            dconn.close()
            if temp_sqlite.exists():
                try:
                    temp_sqlite.unlink()
                except Exception:
                    pass

        return {"status": "success", "counts": sqlite_stats, "target": str(target_path)}

    # Target 3: Partitioned Parquet (Snappy, Snappy/GZIP compressed per sport)

    def export_parquet(
        self,
        out_dir: Optional[str] = None,
        channels: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Exports data into partitioned Parquet files via pandas."""
        try:
            import pandas as pd
        except ImportError:
            return {"status": "error", "message": "pandas package is not installed"}

        target_base = Path(out_dir) if out_dir else self.analytics_dir / "parquet"
        target_base.mkdir(parents=True, exist_ok=True)

        temp_sqlite = self.analytics_dir / "_temp_parquet.db"
        sqlite_stats = self.export_sqlite(db_path=str(temp_sqlite), channels=channels)

        conn = sqlite3.connect(str(temp_sqlite))
        try:
            for table in ["articles", "youtube_videos", "youtube_comments", "tweets"]:
                df = pd.read_sql_query(f"SELECT * FROM {table}", conn)
                table_dir = target_base / table
                table_dir.mkdir(parents=True, exist_ok=True)
                parquet_file = table_dir / "data.parquet"
                df.to_parquet(parquet_file, index=False)
        finally:
            conn.close()
            if temp_sqlite.exists():
                try:
                    temp_sqlite.unlink()
                except Exception:
                    pass

        return {"status": "success", "counts": sqlite_stats, "target": str(target_base)}

    # Unified Runner

    def export(
        self,
        format_type: str = "sqlite",
        output_path: Optional[str] = None,
        channels: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Unified dispatch to analytical format exporter."""
        fmt = format_type.lower()
        if fmt == "sqlite":
            stats = self.export_sqlite(db_path=output_path, channels=channels)
            dest = output_path or str(self.analytics_dir / "sports_analytics.db")
            return {"status": "success", "format": "sqlite", "counts": stats, "target": dest}
        elif fmt == "duckdb":
            return self.export_duckdb(db_path=output_path, channels=channels)
        elif fmt == "parquet":
            return self.export_parquet(out_dir=output_path, channels=channels)
        else:
            return {"status": "error", "message": f"Unsupported format '{format_type}'. Use sqlite, duckdb, or parquet."}

def run_export(
    format_type: str = "sqlite",
    raw_data_dir: str = "data/raw",
    analytics_dir: str = "data/analytics",
    output_path: Optional[str] = None,
    channels: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Convenience functional interface for CLI and test runners."""
    exporter = SportsAnalyticsExporter(raw_data_dir=raw_data_dir, analytics_dir=analytics_dir)
    return exporter.export(format_type=format_type, output_path=output_path, channels=channels)