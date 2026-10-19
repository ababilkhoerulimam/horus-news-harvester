"""
Unit tests for channels and backend fallback routing.
"""

import unittest
from channels import get_all_channels, get_channel
from channels.base import SportsChannel
from channels.portal_news import PortalNewsChannel
from channels.social_youtube import YouTubeChannel
from backends.jina_reader import is_antibot_page
from processors.cleaner import clean_news_text, deduplicate_articles


class TestSportsChannels(unittest.TestCase):

    def test_channel_registry(self):
        channels = get_all_channels()
        self.assertGreaterEqual(len(channels), 2)
        portal_ch = get_channel("portal_news")
        self.assertIsNotNone(portal_ch)
        self.assertIsInstance(portal_ch, SportsChannel)

        yt_ch = get_channel("social_youtube")
        self.assertIsNotNone(yt_ch)
        self.assertIsInstance(yt_ch, YouTubeChannel)

    def test_youtube_channel_methods(self):
        yt_ch = YouTubeChannel()
        self.assertEqual(yt_ch.backends, ["innertube_api", "yt_dlp", "jina_reader"])
        self.assertTrue(yt_ch.can_handle("https://www.youtube.com/watch?v=abcd1234567"))
        self.assertTrue(yt_ch.can_handle("https://youtu.be/abcd1234567"))
        self.assertEqual(yt_ch._extract_video_id("https://www.youtube.com/watch?v=abcd1234567"), "abcd1234567")

    def test_ordered_backends(self):
        ch = PortalNewsChannel()
        self.assertEqual(ch.backends, ["rss_parser", "jina_reader", "direct_http"])

        # Test override
        cfg = {"portal_news_backend": "jina_reader"}
        ordered = ch.ordered_backends(cfg)
        self.assertEqual(ordered[0], "jina_reader")

    def test_antibot_page_detection(self):
        sample_cf = b"<html><head><title>Attention Required! | Cloudflare</title></head></html>"
        self.assertTrue(is_antibot_page(sample_cf))

        sample_clean = b"<html><body><h1>Berita Olahraga Terbaru</h1></body></html>"
        self.assertFalse(is_antibot_page(sample_clean))

    def test_clean_news_text(self):
        raw = "Persib menang telak.\n\nBaca juga: Simak jadwal lengkap Liga 1\n\nHalaman selanjutnya..."
        cleaned = clean_news_text(raw)
        self.assertNotIn("Baca juga", cleaned)
        self.assertNotIn("Halaman selanjutnya", cleaned)
        self.assertIn("Persib menang telak.", cleaned)

    def test_deduplicate_articles(self):
        arts = [
            {"title": "Dewa United Menang", "url": "https://example.com/1"},
            {"title": "Dewa United Menang!", "url": "https://example.com/2"},
            {"title": "Dewa United Kalah", "url": "https://example.com/3"},
        ]
        deduped = deduplicate_articles(arts)
        self.assertEqual(len(deduped), 2)

    def test_targets_and_catalog_files(self):
        import json
        import yaml
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        catalog_file = root / "config" / "portals_catalog.json"
        targets_file = root / "config" / "sports_targets.yaml"

        self.assertTrue(catalog_file.exists())
        self.assertTrue(targets_file.exists())

        with open(catalog_file, "r", encoding="utf-8") as f:
            cat = json.load(f)
            self.assertEqual(cat.get("total_distinct_portals"), 66)
            self.assertEqual(len(cat.get("portals", [])), 66)

        with open(targets_file, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
            self.assertIn("influencers", cfg.get("social_channels", {}))
            self.assertIn("communities", cfg.get("social_channels", {}))
            self.assertIn("football", cfg.get("sports", {}))


if __name__ == "__main__":
    unittest.main()
