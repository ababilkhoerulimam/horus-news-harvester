"""
Unit tests for Operational Hardening:
1. Sub-page archive pagination and early exit
2. Cloudflare Turnstile / 403 challenge detection and headless fallback
3. Dynamic proxy injection and CLI configuration
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from channels.portal_news import (
    PortalNewsChannel,
    is_cloudflare_challenge,
)
from processors.http_client import ResilientHttpClient
from scripts.sitemap_backfill import (
    fetch_archive_articles,
    generate_archive_page_url,
)


class TestArchiveSubPagePagination(unittest.TestCase):
    """Verifies sub-page URL generation and early-exit pagination."""

    def test_generate_archive_page_url(self):
        # Detik
        detik_base = "https://sport.detik.com/sepakbola/indeks?date=10%2F18%2F2026"
        self.assertEqual(generate_archive_page_url(detik_base, "detik", 1), detik_base)
        self.assertEqual(generate_archive_page_url(detik_base, "detik", 2), f"{detik_base}&page=2")

        # Sindonews
        sindo_base = "https://sports.sindonews.com/indeks/0?t=2026-10-18"
        self.assertEqual(generate_archive_page_url(sindo_base, "sindonews", 2), f"{sindo_base}&p=2")

        # Kompas
        kompas_base = "https://bola.kompas.com/search/2026-10-18"
        self.assertEqual(generate_archive_page_url(kompas_base, "kompas", 2), f"{kompas_base}/2")

        # Antara
        antara_base = "https://www.antaranews.com/sepak-bola/arsip/2026-10-18"
        self.assertEqual(generate_archive_page_url(antara_base, "antara", 2), f"{antara_base}/2")

    @patch("scripts.sitemap_backfill.default_http_client.fetch")
    def test_fetch_archive_pagination_early_exit(self, mock_fetch):
        """Ensures crawler stops requesting sub-pages when a page yields 0 new articles."""
        page1_html = """
        <div>
            <a href="https://sport.detik.com/sepakbola/liga-indonesia/d-101/berita-satu">Berita Satu</a>
            <a href="https://sport.detik.com/sepakbola/liga-indonesia/d-102/berita-dua">Berita Dua</a>
        </div>
        """
        page2_html = "<div><p>Tidak ada berita ditemukan</p></div>"

        mock_fetch.side_effect = [
            (200, page1_html.encode("utf-8"), {}),
            (200, page2_html.encode("utf-8"), {}),
        ]

        items = fetch_archive_articles(
            portal="detik",
            start_date="2026-10-18",
            end_date="2026-10-18",
            max_articles=50,
            max_pages_per_date=3
        )

        self.assertEqual(len(items), 2)
        # Should only have made 2 calls (page 1, page 2 empty -> break, does not call page 3)
        self.assertEqual(mock_fetch.call_count, 2)


class TestCloudflareDetectionAndFallback(unittest.TestCase):
    """Verifies Cloudflare signature detection and auto-escalation to Headless Chromium."""

    def test_is_cloudflare_challenge_detection(self):
        # Turnstile challenge HTML
        cf_html = "<html><title>Just a moment...</title><body><div id='cf-turnstile'></div></body></html>"
        self.assertTrue(is_cloudflare_challenge(403, cf_html))
        self.assertTrue(is_cloudflare_challenge(503, "Checking your browser before accessing..."))
        self.assertTrue(is_cloudflare_challenge(200, cf_html))

        # Normal article HTML
        normal_html = "<html><body><h1>Persib Juara Liga</h1><p>Artikel lengkap...</p></body></html>"
        self.assertFalse(is_cloudflare_challenge(200, normal_html))

    @patch("channels.portal_news.default_http_client.fetch")
    def test_portal_news_channel_cloudflare_headless_fallback(self, mock_fetch):
        """When direct HTTP encounters Cloudflare 403/Turnstile, it escalates to headless browser."""
        channel = PortalNewsChannel()

        # Mock direct HTTP returning Cloudflare challenge
        cf_html = "<html><title>Just a moment...</title><body>challenge-running</body></html>"
        mock_fetch.return_value = (403, cf_html.encode("utf-8"), {})

        # Mock Headless Browser resolving the challenge successfully
        channel.headless.extract = MagicMock(return_value={
            "url": "https://sport.detik.com/sepakbola/d-999/test-cf",
            "content": "Isi berita penting berhasil diambil setelah Cloudflare lolos.",
            "backend": "headless_browser",
            "status": "success"
        })

        res = channel.extract(
            "https://sport.detik.com/sepakbola/d-999/test-cf",
            config={"order": ["direct_http", "headless_browser"]}
        )

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("backend"), "headless_browser")
        channel.headless.extract.assert_called_once()


class TestDynamicProxyInjection(unittest.TestCase):
    """Verifies single proxy locking and dynamic proxy pool loading."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.proxy_file = Path(self.temp_dir.name) / "proxies.txt"
        with open(self.proxy_file, "w", encoding="utf-8") as f:
            f.write("http://10.0.0.1:8080\nhttp://10.0.0.2:8080\n# comment\n")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_single_proxy_override(self):
        client = ResilientHttpClient(proxies_file=self.proxy_file, enable_jitter=False)
        self.assertIn(client.get_active_proxy(), ["http://10.0.0.1:8080", "http://10.0.0.2:8080"])

        # Set single proxy
        client.set_single_proxy("http://user:pass@127.0.0.1:9999")
        self.assertEqual(client.get_active_proxy(), "http://user:pass@127.0.0.1:9999")

        # Clear single proxy
        client.set_single_proxy(None)
        self.assertIn(client.get_active_proxy(), ["http://10.0.0.1:8080", "http://10.0.0.2:8080"])

    def test_load_proxies_from_file_and_list(self):
        client = ResilientHttpClient(proxies_file=Path("non_existent.txt"), enable_jitter=False)
        self.assertIsNone(client.get_active_proxy())

        # Load from file
        loaded = client.load_proxies_from_file(str(self.proxy_file))
        self.assertEqual(loaded, 2)
        self.assertIsNotNone(client.get_active_proxy())

        # Add from list
        added = client.add_proxies(["http://10.0.0.3:8080", "http://10.0.0.1:8080"])
        self.assertEqual(added, 1)  # Only 1 unique new proxy added


if __name__ == "__main__":
    unittest.main()
