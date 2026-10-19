"""
Unit tests for SportsAnalyticsExporter (processors/exporter.py).
Verifies SQLite schema creation, PRAGMA optimizations, index generation,
nested YouTube reply flattening, and channel filtering.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from processors.exporter import SportsAnalyticsExporter, run_export


class TestSportsAnalyticsExporter(unittest.TestCase):
    """Test suite for analytical export pipeline."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)
        self.raw_dir = self.base_path / "raw"
        self.analytics_dir = self.base_path / "analytics"

        # Create raw channel directories
        (self.raw_dir / "news").mkdir(parents=True, exist_ok=True)
        (self.raw_dir / "youtube").mkdir(parents=True, exist_ok=True)
        (self.raw_dir / "twitter").mkdir(parents=True, exist_ok=True)

        # 1. Create mock news JSONL
        news_file = self.raw_dir / "news" / "football.jsonl"
        with open(news_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "url": "https://example.com/art1",
                "title": "Timnas Menang Telak 3-0",
                "published_date": "2026-10-18 10:00:00",
                "summary": "Ringkasan kemenangan",
                "author": "PunditBola",
                "source": "antara",
                "backend": "direct_http",
                "sport": "football",
                "content": "Konten berita olahraga lengkap dan mendalam.",
                "extracted_via": "direct_http"
            }) + "\n")
            f.write(json.dumps({
                "url": "https://example.com/art2",
                "title": "Persib Amankan Tiga Poin",
                "published_date": "2026-10-18 15:30:00",
                "summary": "Persib unggul tipis",
                "author": "WartawanSport",
                "source": "kompas",
                "backend": "rss_parser",
                "sport": "football",
                "content": "Laga berjalan sengit hingga peluit akhir.",
                "extracted_via": "direct_http"
            }) + "\n")

        # 2. Create mock YouTube JSONL with nested replies
        yt_file = self.raw_dir / "youtube" / "football.jsonl"
        with open(yt_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "id": "vid_abc123",
                "video_id": "vid_abc123",
                "title": "Highlight Final Liga",
                "url": "https://youtube.com/watch?v=vid_abc123",
                "author": "IndosiarOfficial",
                "published_date": "2 hari yang lalu",
                "sport": "football",
                "source": "youtube",
                "backend": "innertube_api",
                "comment_count": 3,
                "description": "Cuplikan gol-gol terbaik pertandingan final.",
                "comments": [
                    {
                        "id": "comm_root_1",
                        "author": "SuporterSejati",
                        "text": "Gol kedua sangat berkelas!",
                        "likes": "120",
                        "timestamp": "1 hari lalu",
                        "reply_count": 2,
                        "replies": [
                            {
                                "id": "comm_rep_1_1",
                                "author": "AnalisTaktik",
                                "text": "Setuju, pergerakan tanpa bolanya cerdas.",
                                "likes": "15",
                                "timestamp": "18 jam lalu"
                            },
                            {
                                "id": "comm_rep_1_2",
                                "author": "PengamatBola",
                                "text": "Kiper juga salah antisipasi.",
                                "likes": "5",
                                "timestamp": "12 jam lalu"
                            }
                        ]
                    }
                ]
            }) + "\n")

        # 3. Create mock Twitter JSONL
        tw_file = self.raw_dir / "twitter" / "football.jsonl"
        with open(tw_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "tweet_id": "999888777",
                "username": "CoachJustinL",
                "author": "Coach Justin",
                "title": "Tweet by @CoachJustinL",
                "text": "Passing akurat adalah kunci transisi cepat. #SepakbolaNasional",
                "url": "https://x.com/CoachJustinL/status/999888777",
                "published_date": "2026-10-18T18:00:00Z",
                "metrics": {"likes": 550, "retweets": 80, "replies": 45},
                "backend": "graphql_api",
                "sport": "football"
            }) + "\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sqlite_export_full(self):
        """Verifies full export of news, youtube videos & comments, and tweets into SQLite."""
        exporter = SportsAnalyticsExporter(
            raw_data_dir=str(self.raw_dir),
            analytics_dir=str(self.analytics_dir)
        )
        res = exporter.export_sqlite()

        self.assertEqual(res["articles"], 2)
        self.assertEqual(res["youtube_videos"], 1)
        self.assertEqual(res["youtube_comments"], 3)  # 1 root + 2 replies
        self.assertEqual(res["tweets"], 1)

        db_file = self.analytics_dir / "sports_analytics.db"
        self.assertTrue(db_file.exists())

        conn = sqlite3.connect(str(db_file))
        try:
            # Check articles
            art_rows = conn.execute("SELECT url, title, sport FROM articles ORDER BY url").fetchall()
            self.assertEqual(len(art_rows), 2)
            self.assertEqual(art_rows[0][0], "https://example.com/art1")
            self.assertEqual(art_rows[1][0], "https://example.com/art2")

            # Check youtube comments relational structure
            root_c = conn.execute("SELECT comment_id, is_reply, parent_id, reply_count FROM youtube_comments WHERE is_reply = 0").fetchall()
            self.assertEqual(len(root_c), 1)
            self.assertEqual(root_c[0][0], "comm_root_1")
            self.assertIsNone(root_c[0][2])
            self.assertEqual(root_c[0][3], 2)

            rep_c = conn.execute("SELECT comment_id, is_reply, parent_id FROM youtube_comments WHERE is_reply = 1 ORDER BY comment_id").fetchall()
            self.assertEqual(len(rep_c), 2)
            self.assertEqual(rep_c[0][0], "comm_rep_1_1")
            self.assertEqual(rep_c[0][2], "comm_root_1")
            self.assertEqual(rep_c[1][0], "comm_rep_1_2")
            self.assertEqual(rep_c[1][2], "comm_root_1")

            # Check tweets
            tw_rows = conn.execute("SELECT tweet_id, username, likes, retweets FROM tweets").fetchall()
            self.assertEqual(len(tw_rows), 1)
            self.assertEqual(tw_rows[0][0], "999888777")
            self.assertEqual(tw_rows[0][1], "CoachJustinL")
            self.assertEqual(tw_rows[0][2], 550)
            self.assertEqual(tw_rows[0][3], 80)

            # Check indexes exist
            indexes = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()]
            self.assertIn("idx_articles_sport", indexes)
            self.assertIn("idx_yt_comm_pid", indexes)
            self.assertIn("idx_tweets_sport", indexes)
        finally:
            conn.close()

    def test_sqlite_export_idempotence(self):
        """Verifies that running export multiple times does not produce duplicate rows (INSERT OR REPLACE)."""
        exporter = SportsAnalyticsExporter(
            raw_data_dir=str(self.raw_dir),
            analytics_dir=str(self.analytics_dir)
        )
        exporter.export_sqlite()
        # Second run
        res2 = exporter.export_sqlite()

        db_file = self.analytics_dir / "sports_analytics.db"
        conn = sqlite3.connect(str(db_file))
        try:
            art_count = conn.execute("SELECT count(*) FROM articles").fetchone()[0]
            self.assertEqual(art_count, 2)
            comm_count = conn.execute("SELECT count(*) FROM youtube_comments").fetchone()[0]
            self.assertEqual(comm_count, 3)
            tw_count = conn.execute("SELECT count(*) FROM tweets").fetchone()[0]
            self.assertEqual(tw_count, 1)
        finally:
            conn.close()

    def test_channel_filter(self):
        """Verifies that passing channels=['news'] skips youtube and twitter."""
        exporter = SportsAnalyticsExporter(
            raw_data_dir=str(self.raw_dir),
            analytics_dir=str(self.analytics_dir)
        )
        res = exporter.export_sqlite(channels=["news"])
        self.assertEqual(res["articles"], 2)
        self.assertEqual(res["youtube_videos"], 0)
        self.assertEqual(res["tweets"], 0)

        db_file = self.analytics_dir / "sports_analytics.db"
        conn = sqlite3.connect(str(db_file))
        try:
            yt_count = conn.execute("SELECT count(*) FROM youtube_videos").fetchone()[0]
            self.assertEqual(yt_count, 0)
        finally:
            conn.close()

    def test_corrupt_jsonl_line_skipped(self):
        """Verifies that malformed JSON lines are skipped gracefully without aborting execution."""
        bad_file = self.raw_dir / "news" / "corrupt.jsonl"
        with open(bad_file, "w", encoding="utf-8") as f:
            f.write("MALFORMED JSON LINE\n")
            f.write(json.dumps({
                "url": "https://example.com/art_valid",
                "title": "Valid News",
                "sport": "corrupt"
            }) + "\n")
            f.write("{unclosed json\n")

        exporter = SportsAnalyticsExporter(
            raw_data_dir=str(self.raw_dir),
            analytics_dir=str(self.analytics_dir)
        )
        res = exporter.export_sqlite(channels=["news"])
        # Previous 2 articles + 1 valid from corrupt.jsonl = 3
        self.assertEqual(res["articles"], 3)

    def test_run_export_helper(self):
        """Verifies functional run_export helper."""
        res = run_export(
            format_type="sqlite",
            raw_data_dir=str(self.raw_dir),
            analytics_dir=str(self.analytics_dir)
        )
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["format"], "sqlite")
        self.assertIn("sports_analytics.db", res["target"])


if __name__ == "__main__":
    unittest.main()
