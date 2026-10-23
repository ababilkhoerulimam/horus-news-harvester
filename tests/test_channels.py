"""
Unit tests for channels and backend fallback routing.
"""

import unittest
import tempfile
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from channels import get_all_channels, get_channel
from channels.base import SportsChannel
from channels.portal_news import (
    PortalNewsChannel,
    normalize_portal_url,
    find_next_page_url,
    extract_clean_article_text,
    consolidate_article_pages,
)
from channels.social_youtube import YouTubeChannel
from channels.social_twitter import TwitterChannel
from backends.jina_reader import is_antibot_page
from backends.headless_browser import HeadlessBrowserBackend, is_spa_skeleton
from processors.cleaner import clean_news_text, deduplicate_articles
from processors.state_store import StateStore
from processors.http_client import ResilientHttpClient, DEFAULT_USER_AGENTS, default_http_client
from scripts.scheduler import HarvestScheduler


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
        self.assertEqual(ch.backends, ["rss_parser", "curl_impersonate", "jina_reader", "direct_http", "headless_browser"])

        # Test override
        cfg = {"portal_news_backend": "jina_reader"}
        ordered = ch.ordered_backends(cfg)
        self.assertEqual(ordered[0], "jina_reader")

    def test_curl_impersonate_backend(self):
        from backends.curl_impersonate import CurlImpersonateBackend
        curl_backend = CurlImpersonateBackend()
        status, msg = curl_backend.check()
        self.assertIn(status, ("ok", "off"))
        if not curl_backend.is_available():
            code, html = curl_backend.fetch("https://example.com")
            self.assertEqual(code, 0)

    def test_normalize_portal_url(self):
        # 1. Detik without single=1
        url_detik = "https://sport.detik.com/sepakbola/liga-indonesia/d-12345/persib-menang"
        self.assertEqual(
            normalize_portal_url(url_detik),
            "https://sport.detik.com/sepakbola/liga-indonesia/d-12345/persib-menang?single=1"
        )
        # Detik already on page 2
        url_detik_p2 = "https://sport.detik.com/sepakbola/liga-indonesia/d-12345/persib-menang/2"
        self.assertEqual(
            normalize_portal_url(url_detik_p2),
            "https://sport.detik.com/sepakbola/liga-indonesia/d-12345/persib-menang?single=1"
        )
        # Detik already has single=1
        url_detik_existing = "https://sport.detik.com/sepakbola/liga-indonesia/d-12345/persib-menang?single=1"
        self.assertEqual(normalize_portal_url(url_detik_existing), url_detik_existing)

        # 2. Kompas ?page=all
        url_kompas = "https://bola.kompas.com/read/2026/10/19/120000/timnas-menang"
        self.assertEqual(
            normalize_portal_url(url_kompas),
            "https://bola.kompas.com/read/2026/10/19/120000/timnas-menang?page=all"
        )
        url_kompas_p2 = "https://bola.kompas.com/read/2026/10/19/120000/timnas-menang?page=2"
        self.assertEqual(
            normalize_portal_url(url_kompas_p2),
            "https://bola.kompas.com/read/2026/10/19/120000/timnas-menang?page=all"
        )

        # 3. Tribun ?page=all
        url_tribun = "https://www.tribunnews.com/superskor/2026/10/19/jadwal-liga-1"
        self.assertEqual(
            normalize_portal_url(url_tribun),
            "https://www.tribunnews.com/superskor/2026/10/19/jadwal-liga-1?page=all"
        )

        # 4. Unknown portal remains untouched
        url_other = "https://www.antaranews.com/berita/12345/olahraga"
        self.assertEqual(normalize_portal_url(url_other), url_other)

    def test_antibot_page_detection(self):
        sample_cf = b"<html><head><title>Attention Required! | Cloudflare</title></head></html>"
        self.assertTrue(is_antibot_page(sample_cf))

        sample_clean = b"<html><body><h1>Berita Olahraga Terbaru</h1></body></html>"
        self.assertFalse(is_antibot_page(sample_clean))

    def test_clean_news_text(self):
        raw = "Persib menang​ telak.\x00\x08\n\nBaca juga: Simak jadwal lengkap Liga 1\n\nHalaman selanjutnya..."
        cleaned = clean_news_text(raw)
        self.assertNotIn("Baca juga", cleaned)
        self.assertNotIn("Halaman selanjutnya", cleaned)
        self.assertNotIn("​", cleaned)
        self.assertNotIn("\x00", cleaned)
        self.assertIn("Persib menang telak.", cleaned)

    def test_headless_browser_stealth_args(self):
        backend = HeadlessBrowserBackend(browser_path="chrome.exe")
        with unittest.mock.patch("os.path.exists", return_value=True), \
             unittest.mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = unittest.mock.MagicMock(returncode=0, stdout="<html><body>OK</body></html>")
            res = backend.extract("https://example.com/sport")
            self.assertEqual(res["status"], "success")
            cmd = mock_run.call_args[0][0]
            self.assertIn("--disable-blink-features=AutomationControlled", cmd)
            self.assertIn("--blink-settings=primaryHoverType=2,availableHoverTypes=2,primaryPointerType=4,availablePointerTypes=4", cmd)
            self.assertNotIn("--disable-extensions", cmd)

    def test_deduplicate_articles(self):
        arts = [
            {"title": "Dewa United Menang", "url": "https://example.com/1"},
            {"title": "Dewa United Menang!", "url": "https://example.com/2"},
            {"title": "Dewa United Kalah", "url": "https://example.com/3"},
        ]
        deduped = deduplicate_articles(arts)
        self.assertEqual(len(deduped), 2)

    def test_twitter_channel(self):
        tw_ch = get_channel("social_twitter")
        self.assertIsNotNone(tw_ch)
        self.assertIsInstance(tw_ch, TwitterChannel)
        self.assertEqual(tw_ch.backends, ["graphql_api", "syndication_api", "jina_reader", "twitter_cli"])
        self.assertTrue(tw_ch.can_handle("https://x.com/idextratime"))
        self.assertTrue(tw_ch.can_handle("https://twitter.com/PSSI"))
        self.assertTrue(tw_ch.can_handle("@CoachJustinL"))
        self.assertEqual(tw_ch._extract_username("@idextratime"), "idextratime")
        self.assertEqual(tw_ch._extract_username("https://x.com/PSSI/status/123"), "PSSI")

    def test_state_store_sqlite(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            store = StateStore(temp_db)
            test_items = [
                {"url": "https://example.com/art1", "title": "Article 1", "content": "News 1"},
                {"url": "https://example.com/art2", "title": "Article 2", "content": "News 2"},
            ]

            # 1. Initially both are unseen
            unseen = store.filter_unseen("portal_news", test_items, id_key="url")
            self.assertEqual(len(unseen), 2)

            # 2. Mark seen
            added = store.mark_seen("portal_news", "football", test_items, id_key="url")
            self.assertEqual(added, 2)

            # 3. Subsequent filter returns empty
            unseen2 = store.filter_unseen("portal_news", test_items, id_key="url")
            self.assertEqual(len(unseen2), 0)
            self.assertTrue(store.is_seen("https://example.com/art1"))

            # 4. Cursor tracking
            store.set_cursor("social_twitter", "football", "idextratime", "184000111222")
            cursor = store.get_cursor("social_twitter", "football", "idextratime")
            self.assertEqual(cursor, "184000111222")

            # 5. Stats
            stats = store.get_stats()
            self.assertEqual(stats["total_items"], 2)
            self.assertEqual(stats["by_channel"].get("portal_news"), 2)
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)

    def test_scheduler_cycle_logic(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            temp_db = tf.name

        try:
            scheduler = HarvestScheduler(
                state_db_path=temp_db,
                news_interval_sec=1000,
                twitter_interval_sec=1000,
                youtube_interval_sec=1000
            )
            # Both due initially (since last_run is 0)
            now = 2000.0
            due_news = (now - scheduler.last_runs["news"]) >= scheduler.news_interval_sec
            self.assertTrue(due_news)
        finally:
            if os.path.exists(temp_db):
                os.remove(temp_db)

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

    def test_youtube_innertube_comment_parsing_modern(self):
        yt_ch = YouTubeChannel()
        sample_page = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {
                                        "commentId": "c_123",
                                        "content": {"content": "Gol indah Timnas!\nBaca juga: berita lain"},
                                        "publishedTime": "1 hari yang lalu",
                                    },
                                    "author": {"displayName": "@GarudaFans"},
                                    "toolbar": {"likeCountNotliked": "450"}
                                }
                            }
                        }
                    ]
                }
            },
            "onResponseReceivedEndpoints": [
                {
                    "appendContinuationItemsAction": {
                        "continuationItems": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {"token": "next_token_abc"}
                                    }
                                }
                            }
                        ]
                    }
                }
            ]
        }
        seen = set()
        comments, next_token = yt_ch._parse_innertube_comments_page(sample_page, max_comments=10, seen_ids=seen)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["id"], "c_123")
        self.assertEqual(comments[0]["author"], "@GarudaFans")
        self.assertEqual(comments[0]["likes"], "450")
        self.assertNotIn("Baca juga", comments[0]["text"])
        self.assertEqual(next_token, "next_token_abc")

    def test_youtube_innertube_comment_parsing_legacy(self):
        yt_ch = YouTubeChannel()
        sample_page = {
            "onResponseReceivedEndpoints": [
                {
                    "appendContinuationItemsCommand": {
                        "continuationItems": [
                            {
                                "commentThreadRenderer": {
                                    "comment": {
                                        "commentRenderer": {
                                            "commentId": "c_legacy_999",
                                            "authorText": {"simpleText": "CoachJustinFans"},
                                            "contentText": {"runs": [{"text": "Taktik babak kedua mantap"}]},
                                            "voteCount": {"simpleText": "120"},
                                            "publishedTimeText": {"simpleText": "2 jam lalu"}
                                        }
                                    }
                                }
                            },
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {"token": "token_legacy_next"}
                                    }
                                }
                            }
                        ]
                    }
                }
            ]
        }
        seen = set()
        comments, next_token = yt_ch._parse_innertube_comments_page(sample_page, max_comments=10, seen_ids=seen)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["id"], "c_legacy_999")
        self.assertEqual(comments[0]["author"], "CoachJustinFans")
        self.assertEqual(comments[0]["text"], "Taktik babak kedua mantap")
        self.assertEqual(comments[0]["likes"], "120")
        self.assertEqual(next_token, "token_legacy_next")

    def test_youtube_comment_continuation_loop(self):
        from unittest.mock import patch, MagicMock
        import json

        yt_ch = YouTubeChannel()

        page1_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {"commentId": "c1", "content": {"content": "Komentar 1"}},
                                    "author": {"displayName": "User1"},
                                    "toolbar": {"likeCountNotliked": "10"}
                                }
                            }
                        }
                    ]
                }
            },
            "onResponseReceivedEndpoints": [
                {
                    "appendContinuationItemsAction": {
                        "continuationItems": [
                            {
                                "continuationItemRenderer": {
                                    "continuationEndpoint": {
                                        "continuationCommand": {"token": "token_page_2"}
                                    }
                                }
                            }
                        ]
                    }
                }
            ]
        }
        page2_data = {
            "frameworkUpdates": {
                "entityBatchUpdate": {
                    "mutations": [
                        {
                            "payload": {
                                "commentEntityPayload": {
                                    "properties": {"commentId": "c2", "content": {"content": "Komentar 2"}},
                                    "author": {"displayName": "User2"},
                                    "toolbar": {"likeCountNotliked": "20"}
                                }
                            }
                        }
                    ]
                }
            },
            "onResponseReceivedEndpoints": []
        }

        mock_resp1 = MagicMock()
        mock_resp1.status = 200
        mock_resp1.read.return_value = json.dumps(page1_data).encode("utf-8")
        mock_resp1.__enter__.return_value = mock_resp1

        mock_resp2 = MagicMock()
        mock_resp2.status = 200
        mock_resp2.read.return_value = json.dumps(page2_data).encode("utf-8")
        mock_resp2.__enter__.return_value = mock_resp2

        with patch("urllib.request.urlopen", side_effect=[mock_resp1, mock_resp2]):
            comments = yt_ch._fetch_innertube_comments(init_token="token_page_1", max_comments=50)

        self.assertEqual(len(comments), 2)
        self.assertEqual(comments[0]["id"], "c1")
        self.assertEqual(comments[1]["id"], "c2")

    def test_http_client_user_agent_rotation(self):
        client = ResilientHttpClient(enable_jitter=False)
        ua = client.get_random_user_agent()
        self.assertIn(ua, DEFAULT_USER_AGENTS)

    def test_http_client_proxy_loading_and_fallback(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
            tf.write("# Comment line\n")
            tf.write("http://127.0.0.1:8080\n")
            tf.write("http://127.0.0.1:8081\n")
            temp_proxy_file = tf.name

        try:
            client = ResilientHttpClient(proxies_file=Path(temp_proxy_file), enable_jitter=False)
            self.assertEqual(len(client._proxies), 2)

            proxy1 = client.get_active_proxy()
            self.assertIn(proxy1, ["http://127.0.0.1:8080", "http://127.0.0.1:8081"])

            # Mark one failed
            client.mark_proxy_failed("http://127.0.0.1:8080")
            self.assertEqual(client.get_active_proxy(), "http://127.0.0.1:8081")

            # Mark both failed -> returns None (falls back to direct IP)
            client.mark_proxy_failed("http://127.0.0.1:8081")
            self.assertIsNone(client.get_active_proxy())
        finally:
            if os.path.exists(temp_proxy_file):
                os.remove(temp_proxy_file)

    def test_http_client_fetch_mock(self):
        from unittest.mock import patch, MagicMock

        client = ResilientHttpClient(enable_jitter=False)
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"<html>ok</html>"
        mock_resp.headers.items.return_value = [("Content-Type", "text/html")]
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, body, headers = client.fetch("https://example.com/test", timeout=5)

        self.assertEqual(status, 200)
        self.assertEqual(body, b"<html>ok</html>")
        self.assertEqual(headers.get("Content-Type"), "text/html")

    def test_is_spa_skeleton_detection(self):
        # 1. SPA skeleton with root div and empty content
        html_spa = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
        self.assertTrue(is_spa_skeleton(html_spa, ""))
        self.assertTrue(is_spa_skeleton(html_spa, "Memuat halaman..."))

        # 2. Next.js skeleton
        html_next = '<html><body><div id="__next"></div><script>window.__INITIAL_STATE__={}</script></body></html>'
        self.assertTrue(is_spa_skeleton(html_next, "Loading..."))

        # 3. Server-side rendered full article (length >= 400 chars)
        long_text = "Persib Bandung berhasil meraih kemenangan penting dalam laga lanjutan kompetisi liga. " * 8
        self.assertFalse(is_spa_skeleton(html_spa, long_text))

        # 4. Standard static page without SPA indicators
        html_static = '<html><body><p>Berita singkat sepakbola.</p></body></html>'
        self.assertFalse(is_spa_skeleton(html_static, "Berita singkat sepakbola."))

    def test_headless_browser_mock_extract(self):
        from unittest.mock import patch, MagicMock

        backend = HeadlessBrowserBackend(browser_path=r"C:\Mock\chrome.exe")

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = '<html><body><article><h1>Timnas Menang 3-0</h1><p>Pertandingan berlangsung sengit dan dramatis.</p></article></body></html>'

        with patch("os.path.exists", return_value=True), patch("subprocess.run", return_value=mock_proc):
            res = backend.extract("https://news.example.com/bola/timnas-menang")

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("backend"), "headless_browser")
        self.assertIn("Timnas Menang 3-0", res.get("content", ""))

    def test_portal_news_spa_fallback_to_headless(self):
        from unittest.mock import patch, MagicMock

        ch = PortalNewsChannel()

        # Mock direct_http to return an empty SPA skeleton
        mock_spa_body = b'<html><body><div id="root"></div><script src="/bundle.js"></script></body></html>'

        # Mock headless extraction
        mock_headless_res = {
            "url": "https://bola.example.com/spa-article",
            "content": "Konten lengkap berita setelah dieksekusi headless browser.",
            "backend": "headless_browser",
            "status": "success",
        }

        with patch("processors.http_client.default_http_client.fetch", return_value=(200, mock_spa_body, {})), \
             patch.object(ch.headless, "extract", return_value=mock_headless_res):
            res = ch.extract("https://bola.example.com/spa-article", config={"portal_news_backend": "direct_http"})

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("backend"), "headless_browser")
        self.assertIn("Konten lengkap berita", res.get("content", ""))


    def test_normalize_portal_url_extended(self):
        # 1. Sindonews
        url_sindo = "https://sports.sindonews.com/read/12345/timnas-indonesia-lolos"
        self.assertEqual(
            normalize_portal_url(url_sindo),
            "https://sports.sindonews.com/read/12345/timnas-indonesia-lolos?page=all"
        )
        url_sindo_p2 = "https://sports.sindonews.com/read/12345/timnas-indonesia-lolos?page=2"
        self.assertEqual(
            normalize_portal_url(url_sindo_p2),
            "https://sports.sindonews.com/read/12345/timnas-indonesia-lolos?page=all"
        )

        # 2. BolaSport
        url_bolasport = "https://www.bolasport.com/read/3112345/persib-juara-liga-1/2"
        self.assertEqual(
            normalize_portal_url(url_bolasport),
            "https://www.bolasport.com/read/3112345/persib-juara-liga-1?page=all"
        )

        # 3. JawaPos
        url_jawapos = "https://www.jawapos.com/sepak-bola/12345/persebaya-menang"
        self.assertEqual(
            normalize_portal_url(url_jawapos),
            "https://www.jawapos.com/sepak-bola/12345/persebaya-menang?page=all"
        )

        # 4. Bola.com & Bola.net
        self.assertEqual(
            normalize_portal_url("https://www.bola.com/indonesia/read/12345/timnas"),
            "https://www.bola.com/indonesia/read/12345/timnas?page=all"
        )
        self.assertEqual(
            normalize_portal_url("https://www.bola.net/indonesia/hasil-pertandingan.html"),
            "https://www.bola.net/indonesia/hasil-pertandingan.html?page=all"
        )

    def test_find_next_page_url(self):
        base_url = "https://sport.example.com/read/2026/10/19/timnas-menang"

        # 1. Canonical rel="next"
        html_rel = '<html><head><link rel="next" href="https://sport.example.com/read/2026/10/19/timnas-menang?page=2"></head></html>'
        self.assertEqual(
            find_next_page_url(html_rel, base_url),
            "https://sport.example.com/read/2026/10/19/timnas-menang?page=2"
        )

        # 2. Relative link with anchor text "Halaman Selanjutnya"
        html_text = '<html><body><a href="?page=2" class="btn">Halaman Selanjutnya</a></body></html>'
        self.assertEqual(
            find_next_page_url(html_text, base_url),
            "https://sport.example.com/read/2026/10/19/timnas-menang?page=2"
        )

        # 3. Numbered pagination block
        html_paging = '<html><body><div class="paging"><span class="active">1</span><a href="/read/2026/10/19/timnas-menang/2">2</a></div></body></html>'
        self.assertEqual(
            find_next_page_url(html_paging, base_url),
            "https://sport.example.com/read/2026/10/19/timnas-menang/2"
        )

        # 4. Single-page content without pagination
        html_single = '<html><body><p>Berita tuntas dalam satu halaman.</p></body></html>'
        self.assertIsNone(find_next_page_url(html_single, base_url))

    def test_extract_clean_article_text(self):
        html_with_noise = """
        <html>
        <head><style>body { font-size: 14px; }</style></head>
        <body>
            <header><nav><a href="/">Home</a><a href="/bola">Bola</a></nav></header>
            <script>var tracker = 12345; window.__INIT__ = {};</script>
            <div class="read__content">
                <p>JAKARTA - Timnas Indonesia meraih kemenangan penting atas lawannya dalam laga persahabatan internasional.</p>
                <p>Pertandingan berlangsung dengan tempo tinggi sejak menit pertama di hadapan ribuan suporter.</p>
            </div>
            <footer><p>Hak Cipta Terpelihara 2026</p></footer>
        </body>
        </html>
        """
        text = extract_clean_article_text(html_with_noise)
        self.assertNotIn("font-size", text)
        self.assertNotIn("window.__INIT__", text)
        self.assertNotIn("Hak Cipta", text)
        self.assertIn("Timnas Indonesia meraih kemenangan", text)
        self.assertIn("tempo tinggi sejak menit pertama", text)

    def test_consolidate_article_pages(self):
        page1 = "JAKARTA - Laga persahabatan berlangsung seru.\n\nTimnas mencetak gol pada menit ke-15 melalui sundulan tajam."
        # Page 2 repeats the lead intro but has novel continuation text
        page2 = "JAKARTA - Laga persahabatan berlangsung seru.\n\nMemasuki babak kedua, pertahanan solid menjaga keunggulan hingga peluit akhir."

        consolidated = consolidate_article_pages([page1, page2])
        self.assertEqual(consolidated.count("Laga persahabatan berlangsung seru"), 1)
        self.assertIn("menit ke-15 melalui sundulan tajam", consolidated)
        self.assertIn("Memasuki babak kedua", consolidated)

    def test_portal_news_multi_page_traversal(self):
        from unittest.mock import patch

        ch = PortalNewsChannel()

        page1_html = """
        <html><body>
            <div class="read__content">
                <p>Halaman pertama memberitakan babak pertama pertandingan sepak bola secara mendalam dan terperinci.</p>
            </div>
            <a href="https://example.com/berita-bola?page=2" rel="next">Halaman Selanjutnya</a>
        </body></html>
        """.encode("utf-8")

        page2_html = """
        <html><body>
            <div class="read__content">
                <p>Halaman kedua menyajikan jalannya babak kedua beserta ulasan strategi pergantian pemain oleh pelatih.</p>
            </div>
        </body></html>
        """.encode("utf-8")

        responses = {
            "https://example.com/berita-bola": (200, page1_html, {}),
            "https://example.com/berita-bola?page=2": (200, page2_html, {}),
        }

        def mock_fetch(url, *args, **kwargs):
            return responses.get(url, (404, b"", {}))

        with patch("processors.http_client.default_http_client.fetch", side_effect=mock_fetch):
            res = ch.extract("https://example.com/berita-bola", config={"portal_news_backend": "direct_http"})

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("backend"), "direct_http")
        self.assertEqual(res.get("pages_harvested"), 2)
        self.assertIn("Halaman pertama memberitakan", res.get("content", ""))
        self.assertIn("Halaman kedua menyajikan", res.get("content", ""))

    def test_twitter_validate_session(self):
        from unittest.mock import patch, MagicMock
        import urllib.error
        from backends.twitter_graphql import TwitterGraphQLBackend

        client = TwitterGraphQLBackend(auth_token="dummy_token", ct0="dummy_ct0")

        # 1. Success validation
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"screen_name": "coachjustin", "protected": false}'
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            self.assertTrue(client.validate_session())

        # 2. Expired session (401 Unauthorized)
        http_err = urllib.error.HTTPError(
            url="https://x.com/i/api/1.1/account/settings.json",
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None
        )
        with patch("urllib.request.urlopen", side_effect=http_err):
            self.assertFalse(client.validate_session())

    def test_twitter_auto_resync_firefox(self):
        from unittest.mock import patch, MagicMock

        tw_ch = TwitterChannel()
        # Set dummy invalid client
        dummy_client = MagicMock()
        dummy_client.validate_session.return_value = False
        tw_ch.graphql_client = dummy_client

        # Mock successful Firefox extraction
        mock_tokens = {"auth_token": "fresh_auth", "ct0": "fresh_ct0", "profile": "default"}
        with patch("channels.social_twitter.extract_firefox_twitter_tokens", return_value=mock_tokens), \
             patch("channels.social_twitter.save_twitter_session") as mock_save, \
             patch("backends.twitter_graphql.TwitterGraphQLBackend.validate_session", return_value=True):
            res = tw_ch.ensure_active_session(force_refresh=True)

        self.assertTrue(res)
        self.assertIsNotNone(tw_ch.graphql_client)
        self.assertEqual(tw_ch.graphql_client.auth_token, "fresh_auth")
        mock_save.assert_called_once()

    def test_twitter_resync_failure_fallback(self):
        from unittest.mock import patch

        tw_ch = TwitterChannel()
        tw_ch.graphql_client = None

        with patch("channels.social_twitter.extract_firefox_twitter_tokens", return_value=None):
            res = tw_ch.ensure_active_session(force_refresh=True)

        self.assertFalse(res)
        self.assertIsNone(tw_ch.graphql_client)
        status, msg = tw_ch.check()
        self.assertEqual(tw_ch.active_backend, "syndication_api")
        self.assertIn("syndication_api", msg)

    def test_youtube_innertube_reply_token_parsing(self):
        yt_ch = YouTubeChannel()
        sample_page = {
            "onResponseReceivedEndpoints": [
                {
                    "appendContinuationItemsCommand": {
                        "continuationItems": [
                            {
                                "commentThreadRenderer": {
                                    "comment": {
                                        "commentRenderer": {
                                            "commentId": "c_root_1",
                                            "authorText": {"simpleText": "PunditBola"},
                                            "contentText": {"runs": [{"text": "Pemain nomor 10 bermain solid."}]},
                                            "voteCount": {"simpleText": "50"},
                                            "publishedTimeText": {"simpleText": "1 hari lalu"}
                                        }
                                    },
                                    "replies": {
                                        "commentRepliesRenderer": {
                                            "contents": [
                                                {
                                                    "continuationItemRenderer": {
                                                        "continuationEndpoint": {
                                                            "continuationCommand": {
                                                                "token": "reply_token_xyz"
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        }
                                    }
                                }
                            }
                        ]
                    }
                }
            ]
        }
        seen = set()
        comments, next_token = yt_ch._parse_innertube_comments_page(sample_page, max_comments=5, seen_ids=seen)
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["id"], "c_root_1")
        self.assertEqual(comments[0].get("_reply_token"), "reply_token_xyz")

    def test_youtube_innertube_fetch_replies(self):
        from unittest.mock import patch
        import json

        yt_ch = YouTubeChannel()
        mock_reply_payload = {
            "onResponseReceivedEndpoints": [
                {
                    "appendContinuationItemsAction": {
                        "continuationItems": [
                            {
                                "commentRenderer": {
                                    "commentId": "c_reply_1",
                                    "authorText": {"simpleText": "FanGaruda"},
                                    "contentText": {"runs": [{"text": "Setuju sekali, visi bermainnya luar biasa."}]},
                                    "voteCount": {"simpleText": "15"},
                                    "publishedTimeText": {"simpleText": "12 jam lalu"}
                                }
                            }
                        ]
                    }
                }
            ]
        }
        mock_body = json.dumps(mock_reply_payload).encode("utf-8")

        with patch("processors.http_client.default_http_client.fetch", return_value=(200, mock_body, {})):
            replies = yt_ch._fetch_innertube_replies("token_test", max_replies=5)

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["id"], "c_reply_1")
        self.assertEqual(replies[0]["author"], "FanGaruda")
        self.assertIn("Setuju sekali", replies[0]["text"])
        self.assertEqual(replies[0]["likes"], "15")

    def test_youtube_yt_dlp_reply_nesting(self):
        from unittest.mock import patch, MagicMock
        import json

        yt_ch = YouTubeChannel()

        mock_info = {
            "title": "Highlight Pertandingan",
            "description": "Deskripsi video",
            "comments": [
                {
                    "id": "root_1",
                    "parent": "root",
                    "author": "UserRoot",
                    "text": "Komentar utama",
                    "like_count": 100,
                    "timestamp": 1700000000
                },
                {
                    "id": "reply_1_1",
                    "parent": "root_1",
                    "author": "UserReply",
                    "text": "Balasan komentar",
                    "like_count": 10,
                    "timestamp": 1700000010
                }
            ]
        }

        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = json.dumps(mock_info)

        with patch("subprocess.run", return_value=mock_proc):
            res = yt_ch.extract("https://www.youtube.com/watch?v=abcd1234567", config={"social_youtube_backend": "yt_dlp"})

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("backend"), "yt_dlp")
        comments = res.get("comments", [])
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["id"], "root_1")
        self.assertEqual(comments[0]["reply_count"], 1)
        self.assertEqual(len(comments[0]["replies"]), 1)
        self.assertEqual(comments[0]["replies"][0]["id"], "reply_1_1")
        self.assertIn("Balasan komentar", comments[0]["replies"][0]["text"])

    def test_ad_domain_filter(self):
        from processors.ad_filter import is_ad_domain

        # Known ad and sponsored tracking networks
        self.assertTrue(is_ad_domain("https://securepubads.g.doubleclick.net/gampad/ads?id=123"))
        self.assertTrue(is_ad_domain("http://taboola.com/feed"))
        self.assertTrue(is_ad_domain("https://sub.mgid.com/gh/link"))
        self.assertTrue(is_ad_domain("outbrain.com"))
        self.assertTrue(is_ad_domain("https://criteo.com/delivery"))
        self.assertTrue(is_ad_domain("popin.cc"))
        self.assertTrue(is_ad_domain("https://adman.id/tracker"))

        # Valid news and sports domains
        self.assertFalse(is_ad_domain("https://sport.detik.com/sepakbola/liga-indonesia"))
        self.assertFalse(is_ad_domain("https://bola.kompas.com/read/2026/10/24/timnas-menang"))
        self.assertFalse(is_ad_domain("https://antaranews.com/olahraga"))
        self.assertFalse(is_ad_domain(""))
        self.assertFalse(is_ad_domain(None))

    def test_strip_ad_links(self):
        from processors.ad_filter import strip_ad_links

        html_with_ads = (
            '<div>'
            '<p>Berita utama liga Indonesia.</p>'
            '<a href="https://taboola.com/native-click">Sponsored Link 1</a>'
            '<p>Timnas latihan taktik.</p>'
            '<a href="https://sport.detik.com/berita-lanjutan">Baca Halaman 2</a>'
            '<a href="https://mgid.com/promo">Sponsored Link 2</a>'
            '</div>'
        )

        sanitized = strip_ad_links(html_with_ads)
        self.assertNotIn("taboola.com", sanitized)
        self.assertNotIn("Sponsored Link 1", sanitized)
        self.assertNotIn("mgid.com", sanitized)
        self.assertNotIn("Sponsored Link 2", sanitized)
        self.assertIn("sport.detik.com/berita-lanjutan", sanitized)
        self.assertIn("Baca Halaman 2", sanitized)
        self.assertIn("Berita utama liga Indonesia", sanitized)

    def test_sitemap_gzip_decompression(self):
        import gzip
        from scripts.sitemap_backfill import _decompress_sitemap_bytes

        raw_xml = "<urlset><url><loc>https://example.com/bola/1</loc></url></urlset>"
        xml_bytes = raw_xml.encode("utf-8")
        gzip_bytes = gzip.compress(xml_bytes)

        # 1. Decompress valid gzip payload
        decompressed = _decompress_sitemap_bytes(gzip_bytes)
        self.assertEqual(decompressed, raw_xml)

        # 2. Decompress plain uncompressed bytes
        decompressed_plain = _decompress_sitemap_bytes(xml_bytes)
        self.assertEqual(decompressed_plain, raw_xml)

        # 3. Graceful handling of corrupt gzip header
        corrupt_gzip = b"\x1f\x8b\x99\x99corruptdata"
        result_corrupt = _decompress_sitemap_bytes(corrupt_gzip)
        self.assertIsInstance(result_corrupt, str)


if __name__ == "__main__":
    unittest.main()

