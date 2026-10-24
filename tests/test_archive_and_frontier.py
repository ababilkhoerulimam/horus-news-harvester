"""
Unit tests for Resumable SQLite CrawlFrontier, Date Archive HTML Index Parser,
and Adaptive HTTP 429 Backoff & User-Agent Rotation.
"""

import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

from processors.crawl_frontier import CrawlFrontier, interleave_by_portal
from processors.http_client import ResilientHttpClient
from scripts.sitemap_backfill import parse_archive_index_html


class TestCrawlFrontier(unittest.TestCase):
    """Verifies SQLite persistent queue behaviors for multi-day/interrupted crawls."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_frontier.db"
        self.frontier = CrawlFrontier(str(self.db_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_init_and_add_candidates(self):
        """Verifies candidate insertion and duplicate deduplication."""
        candidates = [
            {"url": "https://sport.detik.com/sepakbola/1", "title": "Article 1", "archive_date": "2026-10-18"},
            {"url": "https://sport.detik.com/sepakbola/2", "title": "Article 2", "archive_date": "2026-10-18"},
            {"url": "https://sport.detik.com/sepakbola/1", "title": "Article 1 Duplicate", "archive_date": "2026-10-18"},
        ]
        added = self.frontier.add_candidates(candidates, portal="detik")
        self.assertEqual(added, 2)

        # Re-adding same candidates results in 0 additions
        added_repeat = self.frontier.add_candidates(candidates, portal="detik")
        self.assertEqual(added_repeat, 0)

    def test_get_pending_batch_and_mark_completed(self):
        """Verifies state transition from pending -> in_progress -> completed."""
        candidates = [
            {"url": "https://sport.detik.com/sepakbola/a", "title": "A"},
            {"url": "https://sport.detik.com/sepakbola/b", "title": "B"},
            {"url": "https://sport.detik.com/sepakbola/c", "title": "C"},
        ]
        self.frontier.add_candidates(candidates, portal="detik")

        batch = self.frontier.get_pending_batch(limit=2, portal="detik")
        self.assertEqual(len(batch), 2)
        urls = [item["url"] for item in batch]
        self.assertIn("https://sport.detik.com/sepakbola/a", urls)

        # Mark in progress
        self.frontier.mark_in_progress(urls)

        # Next pending batch only returns remaining item 'c'
        next_batch = self.frontier.get_pending_batch(limit=2, portal="detik")
        self.assertEqual(len(next_batch), 1)
        self.assertEqual(next_batch[0]["url"], "https://sport.detik.com/sepakbola/c")

        # Mark 'a' completed
        self.frontier.mark_completed("https://sport.detik.com/sepakbola/a")
        summary = self.frontier.get_frontier_summary()
        self.assertEqual(summary["totals"]["completed"], 1)

    def test_mark_failed_and_retry_increment(self):
        """Verifies failed items increment retry count and remain eligible until limit."""
        cand = [{"url": "https://sport.detik.com/sepakbola/fail", "title": "Fail"}]
        self.frontier.add_candidates(cand, portal="detik")

        self.frontier.mark_failed("https://sport.detik.com/sepakbola/fail", error="HTTP 500")
        batch = self.frontier.get_pending_batch(limit=1)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["retry_count"], 1)

        # Fail 2 more times (total 3)
        self.frontier.mark_failed("https://sport.detik.com/sepakbola/fail", error="HTTP 500")
        self.frontier.mark_failed("https://sport.detik.com/sepakbola/fail", error="HTTP 500")

        # After 3 failures, should no longer be in pending batch
        batch_exhausted = self.frontier.get_pending_batch(limit=1)
        self.assertEqual(len(batch_exhausted), 0)

    def test_reset_in_progress_on_resume(self):
        """Verifies previously in-flight tasks are restored on resume."""
        cand = [{"url": "https://sport.detik.com/sepakbola/resume_test", "title": "Resume"}]
        self.frontier.add_candidates(cand, portal="detik")
        self.frontier.mark_in_progress(["https://sport.detik.com/sepakbola/resume_test"])

        reset_count = self.frontier.reset_in_progress()
        self.assertEqual(reset_count, 1)

        batch = self.frontier.get_pending_batch(limit=1)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["url"], "https://sport.detik.com/sepakbola/resume_test")

    def test_claim_pending_batch_atomic(self):
        """Verifies multi-core worker atomic claiming without URL race condition."""
        candidates = [
            {"url": "https://sport.detik.com/core/1", "title": "1"},
            {"url": "https://sport.detik.com/core/2", "title": "2"},
            {"url": "https://sport.detik.com/core/3", "title": "3"},
        ]
        self.frontier.add_candidates(candidates, portal="detik")

        # Core-0 claims 2 items
        core0_batch = self.frontier.claim_pending_batch(limit=2)
        self.assertEqual(len(core0_batch), 2)
        core0_urls = {item["url"] for item in core0_batch}

        # Core-1 claims remaining items -> receives only the 3rd item
        core1_batch = self.frontier.claim_pending_batch(limit=2)
        self.assertEqual(len(core1_batch), 1)
        core1_urls = {item["url"] for item in core1_batch}

        # Zero intersection between core allocations
        self.assertEqual(len(core0_urls.intersection(core1_urls)), 0)
        self.assertIn("https://sport.detik.com/core/3", core1_urls)

    def test_batch_commit_buffering_and_flush(self):
        """Verifies in-memory buffer delays write until batch limit or explicit flush."""
        candidates = [{"url": f"https://sport.detik.com/buf/{i}", "title": str(i)} for i in range(10)]
        self.frontier.add_candidates(candidates, portal="detik")
        self.frontier.claim_pending_batch(limit=10)

        # Mark 5 completed (below batch_size=50)
        for i in range(5):
            self.frontier.mark_completed(f"https://sport.detik.com/buf/{i}")

        # Items should be buffered in memory
        self.assertEqual(len(self.frontier._completed_buffer), 5)

        # Explicit flush writes to DB and clears buffer
        self.frontier.flush()
        self.assertEqual(len(self.frontier._completed_buffer), 0)

        summary = self.frontier.get_frontier_summary()
        self.assertEqual(summary["totals"]["completed"], 5)


class TestArchiveIndexParser(unittest.TestCase):
    """Verifies date archive HTML index parsing across major Indonesian portals."""

    def test_parse_detik_archive_html(self):
        """Ensures Detik date index article links are extracted and normalized."""
        mock_detik_html = """
        <div class="list media_rows">
            <article>
                <a href="https://sport.detik.com/sepakbola/liga-indonesia/d-7589123/persib-kalahkan-arema-di-bandung">
                    <h2>Persib Kalahkan Arema di Bandung Lewat Gol Telat</h2>
                </a>
            </article>
            <article>
                <a href="https://sport.detik.com/sepakbola/timnas/d-7589124/shin-tae-yong-matangkan-taktik-garuda">
                    <h2>Shin Tae-yong Matangkan Taktik Garuda Jelang Laga Krusial</h2>
                </a>
            </article>
            <div class="ad-banner">
                <a href="https://promo.detik.com/diskon">Iklan Diskon</a>
            </div>
        </div>
        """
        items = parse_archive_index_html(mock_detik_html, portal="detik", archive_date="2026-10-18")
        self.assertEqual(len(items), 2)
        urls = [i["url"] for i in items]
        self.assertTrue(any("d-7589123" in u for u in urls))
        self.assertTrue(any("d-7589124" in u for u in urls))
        self.assertFalse(any("promo.detik.com" in u for u in urls))

    def test_parse_antara_and_kompas_archive_html(self):
        """Ensures Antara and Kompas sports archive links are extracted."""
        mock_antara_html = """
        <div class="main-content">
            <article>
                <a href="https://www.antaranews.com/berita/4401234/timnas-futsal-indonesia-lolos-ke-semifinal">
                    Timnas Futsal Indonesia Lolos ke Semifinal Piala Asia
                </a>
            </article>
        </div>
        """
        antara_items = parse_archive_index_html(mock_antara_html, portal="antara", archive_date="2026-10-18")
        self.assertEqual(len(antara_items), 1)
        self.assertIn("antaranews.com/berita/4401234", antara_items[0]["url"])

        mock_kompas_html = """
        <div class="article__list">
            <a href="https://bola.kompas.com/read/2026/10/18/1430001/klasemen-liga-1-persib-puncaki-tabel">
                Klasemen Liga 1: Persib Puncaki Tabel
            </a>
        </div>
        """
        kompas_items = parse_archive_index_html(mock_kompas_html, portal="kompas", archive_date="2026-10-18")
        self.assertEqual(len(kompas_items), 1)
        self.assertIn("bola.kompas.com/read/2026/10/18", kompas_items[0]["url"])


class TestHttpClientAdaptiveBackoff(unittest.TestCase):
    """Verifies HTTP 429 adaptive retry and User-Agent rotation."""

    def test_http_429_backoff_and_recovery(self):
        """Ensures client sleeps on 429 and succeeds on subsequent retry."""
        client = ResilientHttpClient(enable_jitter=False)

        # Mock urllib.request.urlopen
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html>OK</html>"
        mock_resp.headers = {"Content-Type": "text/html"}
        mock_resp.__enter__.return_value = mock_resp

        # Create 429 HTTPError
        err_429 = urllib.error.HTTPError(
            url="https://example.com",
            code=429,
            msg="Too Many Requests",
            hdrs={"Retry-After": "1"},
            fp=None
        )

        with patch("urllib.request.urlopen") as mock_urlopen, patch("time.sleep") as mock_sleep:
            mock_urlopen.side_effect = [err_429, mock_resp]
            status, body, _ = client.fetch("https://example.com/test", max_retries=2)

            self.assertEqual(status, 200)
            self.assertEqual(body, b"<html>OK</html>")
            self.assertEqual(mock_urlopen.call_count, 2)
            self.assertTrue(mock_sleep.called)


class TestInterleaveByPortal(unittest.TestCase):
    """Verifies round-robin interleaving across portal domains."""

    def test_interleave_basic(self):
        items = [
            {"url": "a1", "portal": "detik"},
            {"url": "a2", "portal": "detik"},
            {"url": "a3", "portal": "detik"},
            {"url": "b1", "portal": "kompas"},
            {"url": "b2", "portal": "kompas"},
            {"url": "c1", "portal": "viva"},
        ]
        res = interleave_by_portal(items)
        expected_urls = ["a1", "b1", "c1", "a2", "b2", "a3"]
        actual_urls = [x["url"] for x in res]
        self.assertEqual(actual_urls, expected_urls)

    def test_interleave_empty_and_single(self):
        self.assertEqual(interleave_by_portal([]), [])
        single = [{"url": "x1", "portal": "p1"}, {"url": "x2", "portal": "p1"}]
        self.assertEqual(interleave_by_portal(single), single)


if __name__ == "__main__":
    unittest.main()
