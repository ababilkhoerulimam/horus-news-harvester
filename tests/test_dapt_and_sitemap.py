"""
Unit tests for Trafilatura text extraction, XML sitemap backfilling,
and NusaBERT DAPT corpus preparation.
"""

import asyncio
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from channels.portal_news import extract_clean_article_text
from doctor import check_packages
from processors.dapt import (
    build_dapt_corpus,
    format_corpus_summary,
    sanitize_dapt_text,
)
from processors.portal_catalog import PortalCatalogRegistry
from scripts.sitemap_backfill import (
    generate_portal_archive_urls,
    parse_sitemap_xml,
)


class TestTrafilaturaCleaner(unittest.TestCase):
    """Verifies clean text extraction without CSS/JS/ad leaks."""

    def test_css_and_script_leak_prevention(self):
        """Ensures CSS blocks, scripts, and ad wrappers are stripped from text."""
        dirty_html = """
        <html>
        <head>
            <style>
                .ads-inline { display: none; color: #ff0000; font-size: 14px; }
                @font-face { font-family: 'CustomFont'; src: url('font.woff'); }
            </style>
            <script>
                (function(w,d,s,l,i){w[l]=w[l]||[];w[l].push({'gtm.start': new Date().getTime()});})(window,document,'script','dataLayer','GTM-XYZ');
            </script>
        </head>
        <body>
            <div class="read__content">
                <p>Persib Bandung berhasil meraih kemenangan penting atas Persebaya Surabaya dalam laga lanjutan Liga 1.</p>
                <div class="ads-inline">Beli tiket sekarang diskon 50%!</div>
                <script>console.log("tracking pixel");</script>
                <p>Pelatih Shin Tae-yong memuji performa para pemain muda yang tampil percaya diri di hadapan puluhan ribu suporter di stadion.</p>
            </div>
            <footer>
                <nav><a href="/privacy">Kebijakan Privasi</a></nav>
            </footer>
        </body>
        </html>
        """
        extracted = extract_clean_article_text(dirty_html)
        self.assertNotIn("gtm.start", extracted)
        self.assertNotIn("@font-face", extracted)
        self.assertNotIn("display: none", extracted)
        self.assertNotIn("Kebijakan Privasi", extracted)
        self.assertIn("Persib Bandung", extracted)
        self.assertIn("Shin Tae-yong", extracted)

    def test_empty_html_returns_empty_string(self):
        """Verifies graceful handling of empty or None HTML."""
        self.assertEqual(extract_clean_article_text(""), "")
        self.assertEqual(extract_clean_article_text(None), "")


class TestSitemapParser(unittest.TestCase):
    """Verifies XML sitemap and archive URL parsing."""

    def test_parse_urlset_with_football_filter(self):
        """Verifies filtering of sports vs non-sports URLs from XML urlset."""
        xml_data = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <url>
                <loc>https://sport.detik.com/sepakbola/liga-indonesia/d-7001/persib-raih-poin-penuh</loc>
                <lastmod>2026-10-18T10:00:00Z</lastmod>
            </url>
            <url>
                <loc>https://finance.detik.com/moneter/d-7002/ihsg-menguat-hari-ini</loc>
                <lastmod>2026-10-18T09:00:00Z</lastmod>
            </url>
            <url>
                <loc>https://sport.detik.com/sepakbola/timnas/d-7003/jadwal-timnas-indonesia</loc>
                <lastmod>2026-10-18T11:00:00Z</lastmod>
            </url>
        </urlset>
        """
        articles, child_sitemaps = parse_sitemap_xml(xml_data)
        self.assertEqual(len(child_sitemaps), 0)
        self.assertEqual(len(articles), 2)
        urls = [a["url"] for a in articles]
        self.assertTrue(any("persib-raih-poin-penuh" in u for u in urls))
        self.assertTrue(any("jadwal-timnas-indonesia" in u for u in urls))
        self.assertFalse(any("ihsg-menguat" in u for u in urls))

    def test_parse_sitemap_index(self):
        """Verifies sitemap index files yield child sitemaps."""
        index_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
            <sitemap>
                <loc>https://www.antaranews.com/sitemap_news.xml</loc>
            </sitemap>
            <sitemap>
                <loc>https://www.antaranews.com/sitemap_sport.xml</loc>
            </sitemap>
        </sitemapindex>
        """
        articles, child_sitemaps = parse_sitemap_xml(index_xml)
        self.assertEqual(len(articles), 0)
        self.assertEqual(len(child_sitemaps), 2)
        self.assertIn("https://www.antaranews.com/sitemap_news.xml", child_sitemaps)

    def test_parse_news_sitemap_preserves_title_and_date(self):
        """Verifies Google News extension metadata survives namespaced parsing."""
        news_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
                xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
            <url>
                <loc>https://www.antaranews.com/berita/123456/sepak-bola-timnas</loc>
                <news:news>
                    <news:publication>
                        <news:name>Antara</news:name>
                        <news:language>id</news:language>
                    </news:publication>
                    <news:publication_date>2026-10-20T10:00:00Z</news:publication_date>
                    <news:title>Timnas Indonesia Menang</news:title>
                </news:news>
            </url>
        </urlset>
        """
        articles, _ = parse_sitemap_xml(news_xml)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["title"], "Timnas Indonesia Menang")
        self.assertEqual(articles[0]["published_date"], "2026-10-20T10:00:00Z")

    def test_generate_portal_archive_urls(self):
        """Verifies date archive URL generator for Detik and Antara."""
        detik_urls = generate_portal_archive_urls("detik", "2026-10-17", "2026-10-19")
        self.assertEqual(len(detik_urls), 3)
        self.assertIn("sport.detik.com/sepakbola/indeks", detik_urls[0])

        antara_urls = generate_portal_archive_urls("antara", "2026-10-18", "2026-10-18")
        self.assertEqual(len(antara_urls), 1)
        self.assertIn("antaranews.com/sepak-bola/arsip/2026-10-18", antara_urls[0])


class TestDAPTCorpusBuilder(unittest.TestCase):
    """Verifies DAPT text sanitization and corpus generation for NusaBERT MLM."""

    def test_sanitize_dapt_text(self):
        """Verifies removal of editorial boilerplates and social clutter."""
        noisy_text = """
        Persija Jakarta menggelar latihan intensif jelang laga derby.
        Baca Juga: Persib Siap Tempur Hadapi Rival Abadi
        Simak Video: Gol Indah Marc Klok Menembus Gawang Lawan
        Pelatih Thomas Doll meminta anak asuhnya fokus 90 menit penuh.
        ADVERTISEMENT
        SCROLL TO CONTINUE WITH CONTENT
        Dukungan The Jakmania sangat berharga bagi moral skuad Macan Kemayoran.
        """
        cleaned = sanitize_dapt_text(noisy_text)
        self.assertNotIn("Baca Juga", cleaned)
        self.assertNotIn("Simak Video", cleaned)
        self.assertNotIn("ADVERTISEMENT", cleaned)
        self.assertNotIn("SCROLL TO CONTINUE", cleaned)
        self.assertIn("Persija Jakarta", cleaned)
        self.assertIn("The Jakmania", cleaned)

    def test_build_dapt_corpus_workflow(self):
        """Verifies full build_dapt_corpus pipeline: filtering short articles and formatting."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            in_file = base / "sample_news.jsonl"
            out_file = base / "corpus.txt"

            long_text = " ".join(["Sepak bola Indonesia mengalami peningkatan pesat dalam ranking FIFA."] * 10)
            short_text = "Berita singkat skor akhir 1-0."

            with open(in_file, "w", encoding="utf-8") as f:
                f.write(json.dumps({"content": long_text, "url": "https://example.com/1"}) + "\n")
                f.write(json.dumps({"content": short_text, "url": "https://example.com/2"}) + "\n")
                # Duplicate entry
                f.write(json.dumps({"content": long_text, "url": "https://example.com/3"}) + "\n")

            stats = build_dapt_corpus(
                input_path=str(in_file),
                output_path=str(out_file),
                min_words=20,
                doc_format="line"
            )

            self.assertEqual(stats["status"], "success")
            self.assertEqual(stats["total_read"], 3)
            self.assertEqual(stats["articles_retained"], 1)
            self.assertEqual(stats["discarded_short"], 1)
            self.assertEqual(stats["discarded_duplicate"], 1)
            self.assertGreater(stats["total_words"], 50)
            self.assertGreater(stats["estimated_tokens"], 60)

            # Check output file has 1 line
            lines = out_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

            summary = format_corpus_summary(stats)
            self.assertIn("NusaBERT DAPT Sports Corpus Preparation Report", summary)


class TestDoctorTrafilatura(unittest.TestCase):
    """Verifies doctor diagnostic package check includes trafilatura."""

    def test_doctor_packages_contains_trafilatura(self):
        pkgs = check_packages()
        self.assertIn("trafilatura", pkgs)


class TestMultiPortalArchiveAndWPRest(unittest.TestCase):
    """Verifies multi-portal archive templates and WordPress REST API crawling."""

    def test_multi_portal_archive_url_generation(self):
        # Okezone
        u_okz = generate_portal_archive_urls("okezone", "2026-04-15", "2026-04-15")
        self.assertEqual(len(u_okz), 1)
        self.assertIn("bola.okezone.com/indeks/2026/04/15/", u_okz[0])

        # Suara
        u_suara = generate_portal_archive_urls("suara", "2026-04-15", "2026-04-15")
        self.assertEqual(len(u_suara), 1)
        self.assertIn("suara.com/indeks/terkini/bola?date=2026-04-15", u_suara[0])

        # Kompas
        u_kompas = generate_portal_archive_urls("kompas", "2026-04-15", "2026-04-15")
        self.assertEqual(len(u_kompas), 1)
        self.assertIn("bola.kompas.com/search/2026-04-15", u_kompas[0])

    @patch("scripts.sitemap_backfill.default_http_client.fetch")
    def test_fetch_wordpress_rest_articles(self, mock_fetch):
        from scripts.sitemap_backfill import fetch_wordpress_rest_articles

        sample_posts = [
            {
                "id": 101,
                "date": "2026-04-15T12:00:00",
                "link": "https://balipost.com/news/2026/04/15/persib-raih-kemenangan.html",
                "title": {"rendered": "Persib Raih Kemenangan Dramatis di Laga Liga 1"},
                "content": {"rendered": "<p>Persib Bandung sukses menaklukkan lawan...</p>"}
            },
            {
                "id": 102,
                "date": "2026-04-15T14:00:00",
                "link": "https://balipost.com/news/2026/04/15/pertandingan-bola-voli.html",
                "title": {"rendered": "Pertandingan Bola Voli Antar Desa Berlangsung Meriah"},
                "content": {"rendered": "<p>Turnamen bola voli ini diikuti puluhan regu...</p>"}
            }
        ]
        # Return posts on page 1, then empty on page 2
        mock_fetch.side_effect = [
            (200, json.dumps(sample_posts).encode("utf-8"), {}),
            (200, b"[]", {})
        ]

        items = fetch_wordpress_rest_articles(
            portal="balipost",
            base_api_url="https://www.balipost.com/wp-json/wp/v2/posts",
            start_date="2026-04-01",
            end_date="2026-04-30",
            max_articles=50
        )

        # Should only retain football post (101), volleyball post (102) excluded
        self.assertEqual(len(items), 1)
        self.assertIn("persib", items[0]["url"])
        self.assertIn("Persib Raih Kemenangan", items[0]["title"])

    def test_run_sitemap_backfill_signature_and_empty_queue(self):
        from scripts.sitemap_backfill import run_sitemap_backfill
        with tempfile.TemporaryDirectory() as td:
            db_file = Path(td) / "temp_frontier.db"
            out_file = Path(td) / "temp_out.jsonl"
            res = run_sitemap_backfill(
                portal="detik",
                resume=True,
                output_file=str(out_file),
                max_articles=10,
                cores=2,
                frontier_db=str(db_file),
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["collected"], 0)


class BackfillProcessItemTests(unittest.TestCase):
    """Pins the record shape shared by the core workers and the in-process runner."""

    class _Sink:
        def __init__(self):
            self.emitted = []
            self.failed = []

        async def emit(self, record):
            self.emitted.append(record)

        async def fail(self, url, reason):
            self.failed.append((url, reason))

    def _run(self, result):
        from scripts.sitemap_backfill import _backfill_process_item

        sink = self._Sink()

        async def fake_fetch(session, backend, channel, url, **kwargs):
            return result

        with patch("scripts.sitemap_backfill.fetch_article", side_effect=fake_fetch):
            process = _backfill_process_item(None, None, None, 150)
            asyncio.run(process({"url": "https://detik.com/a", "title": "Judul"}, sink))
        return sink

    def test_success_emits_the_news_record_schema(self):
        from processors.fetch_pipeline import FetchResult

        sink = self._run(
            FetchResult("x" * 20, "direct_http", None, "Judul", "2026-01-01T00:00:00Z", 20, "low")
        )

        self.assertEqual(sink.failed, [])
        self.assertEqual(len(sink.emitted), 1)
        self.assertEqual(
            set(sink.emitted[0]),
            {
                "url",
                "title",
                "published_date",
                "archive_date",
                "content",
                "source",
                "backend",
                "quality",
                "content_chars",
                "harvested_at",
            },
        )
        self.assertEqual(sink.emitted[0]["quality"], "low")
        self.assertEqual(sink.emitted[0]["content_chars"], 20)

    def test_failed_fetch_reports_the_reason_without_emitting(self):
        from processors.fetch_pipeline import FetchResult

        sink = self._run(FetchResult(None, None, "HTTP 404 Not Found"))

        self.assertEqual(sink.emitted, [])
        self.assertEqual(sink.failed, [("https://detik.com/a", "HTTP 404 Not Found")])


class ChannelExtractRecordTests(unittest.TestCase):
    """Pins the thread fallback: normalize, gate, then build the record."""

    class _Channel:
        def __init__(self, result):
            self.result = result
            self.calls = 0

        def extract(self, url, config=None):
            self.calls += 1
            return self.result

    def _run(self, result, min_content_length=150):
        from scripts.sitemap_backfill import _channel_extract_record

        channel = self._Channel(result)
        return _channel_extract_record(
            {"url": "https://detik.com/a", "title": "Judul"},
            channel,
            min_content_length,
        )

    def test_dateline_is_removed_before_the_record_is_built(self):
        record, error = self._run({
            "status": "success",
            "content": "Jakarta - " + "Laga berjalan sengit. " * 10,
            "backend": "direct_http",
        })

        self.assertIsNone(error)
        self.assertTrue(record["content"].startswith("Laga berjalan"))
        self.assertNotIn("Jakarta", record["content"])
        self.assertEqual(record["content_chars"], len(record["content"]))
        self.assertEqual(record["title"], "Judul")

    def test_a_page_that_is_only_a_dateline_fails_the_gate(self):
        from processors.fetch_pipeline import REASON_TOO_SHORT

        record, error = self._run({
            "status": "success",
            "content": "Jakarta - Laga berjalan sengit.",
            "backend": "direct_http",
        }, min_content_length=25)

        self.assertIsNone(record)
        self.assertEqual(error, REASON_TOO_SHORT)

    def test_a_failed_extraction_reports_its_own_reason(self):
        record, error = self._run({"status": "failed", "content": "", "error": "blocked"})

        self.assertIsNone(record)
        self.assertEqual(error, "blocked")

    def test_channel_extract_record_rejects_a_reader_envelope(self):
        from processors.fetch_pipeline import REASON_REJECTED

        record, error = self._run({
            "status": "success",
            "content": "Title: X\nURL Source: https://a\n\nMarkdown Content:\n\n" + "isi " * 80,
            "backend": "direct_http",
        }, min_content_length=1)

        self.assertIsNone(record)
        self.assertEqual(error, REASON_REJECTED)

    def test_channel_extract_record_passes_quality_explicitly(self):
        # A caption-shaped body is "low" from its shape, not from being short:
        # build_news_record would call it "ok" because 250 characters is not
        # under its length threshold, so the explicit quality is the only thing
        # that carries the decision.
        record, error = self._run({
            "status": "success",
            "content": "Pemain Persib berlatih di stadion jelang laga. (BP/Antara)",
            "backend": "direct_http",
        }, min_content_length=1)

        self.assertIsNone(error)
        self.assertEqual(record["quality"], "low")


class SitemapPreferenceTests(unittest.TestCase):
    """WordPress's own sitemap must beat a stale root /sitemap.xml.

    niaga.asia's fingerprint pointed at a 453-byte export that listed only the
    homepage, so every backfill for that portal found zero articles.
    """

    def _resolve(self, active_sitemaps):
        with tempfile.TemporaryDirectory() as tmp:
            fingerprints = Path(tmp) / "fingerprints.json"
            fingerprints.write_text(
                json.dumps([{"domain": "example.com", "active_sitemaps": active_sitemaps}]),
                encoding="utf-8",
            )
            registry = PortalCatalogRegistry(
                catalog_file=Path(tmp) / "absent_catalog.json",
                fingerprints_file=fingerprints,
            )
        return registry.resolve_domain("example.com")

    def test_wp_sitemap_is_preferred_over_a_stale_entry(self):
        portal = self._resolve([
            "https://example.com/sitemap.xml",
            "https://example.com/wp-sitemap.xml",
        ])

        self.assertEqual(portal.sitemap_url, "https://example.com/wp-sitemap.xml")

    def test_news_sitemap_keeps_priority_over_wp_sitemap(self):
        portal = self._resolve([
            "https://example.com/sitemap-news.xml",
            "https://example.com/wp-sitemap.xml",
        ])

        self.assertEqual(portal.sitemap_url, "https://example.com/sitemap-news.xml")

    def test_first_entry_is_used_when_no_wp_sitemap_exists(self):
        portal = self._resolve([
            "https://example.com/sitemap_index.xml",
            "https://example.com/post-sitemap.xml",
        ])

        self.assertEqual(portal.sitemap_url, "https://example.com/sitemap_index.xml")

    def test_missing_active_sitemaps_falls_back_to_sitemap_xml(self):
        portal = self._resolve([])

        self.assertEqual(portal.sitemap_url, "https://example.com/sitemap.xml")


class SitemapCandidateTests(unittest.TestCase):
    """Both discovery paths must probe wp-sitemap.xml.

    Without it a WordPress 5.5+ portal that serves only wp-sitemap.xml is
    invisible to discovery, which is the state niaga.asia was in.
    """

    def test_fingerprint_candidates_include_wp_sitemap(self):
        # Imported here because the script configures root logging on import.
        from scripts.fingerprint_portals import candidate_sitemaps

        candidates = candidate_sitemaps("https://www.example.com", "example.com")

        self.assertIn("https://www.example.com/wp-sitemap.xml", candidates)
        self.assertIn("https://example.com/wp-sitemap.xml", candidates)

    def test_catalog_discovery_candidates_include_wp_sitemap(self):
        from scripts.discover_catalog_portals import standard_sitemap_candidates

        candidates = standard_sitemap_candidates("https://sport.example.com", "example.com")

        self.assertIn("https://sport.example.com/wp-sitemap.xml", candidates)
        self.assertIn("https://example.com/wp-sitemap.xml", candidates)

    def test_catalog_discovery_still_falls_back_to_sitemap_xml(self):
        from scripts.discover_catalog_portals import standard_sitemap_candidates

        candidates = standard_sitemap_candidates("https://sport.example.com", "example.com")

        self.assertEqual(candidates[0], "https://sport.example.com/sitemap.xml")


class CatalogDiscoveryTests(unittest.TestCase):
    """run_catalog_discovery must not re-walk sitemaps after the budget is met.

    Each candidate is a full recursive walk, so without the guard a portal that
    already reached max_urls_per_portal keeps fetching the remaining candidates.
    """

    @staticmethod
    def _articles(tag, count):
        return [
            {
                "url": f"https://example.com/{tag}/{i}",
                "title": "",
                "published_date": "",
                "archive_date": "",
            }
            for i in range(count)
        ]

    def _run(self, walk_side_effect, max_urls=5000, fingerprints=None, wp_side_effect=None):
        from scripts.discover_catalog_portals import run_catalog_discovery

        with tempfile.TemporaryDirectory() as tmp:
            catalog = Path(tmp) / "catalog.json"
            catalog.write_text(
                json.dumps({
                    "portals": [{
                        "domain": "example.com",
                        "name": "Example",
                        "sports_url": "https://sport.example.com",
                        "has_sports_desk": False,
                    }]
                }),
                encoding="utf-8",
            )
            fingerprints_path = Path(tmp) / "fingerprints.json"
            fingerprints_path.write_text(json.dumps(fingerprints or []), encoding="utf-8")

            with patch("scripts.discover_catalog_portals.configure_proxy", return_value=(0, None)), \
                    patch("scripts.discover_catalog_portals.fetch_sitemap_urls") as walk, \
                    patch("scripts.discover_catalog_portals.fetch_wordpress_rest_articles") as wp:
                walk.side_effect = walk_side_effect
                wp.side_effect = wp_side_effect or (lambda **kwargs: [])
                with redirect_stdout(io.StringIO()):
                    result = run_catalog_discovery(
                        catalog_file=str(catalog),
                        fingerprints_file=str(fingerprints_path),
                        frontier_db=str(Path(tmp) / "frontier.db"),
                        max_urls_per_portal=max_urls,
                    )

        return result, walk.call_args_list

    def _candidate_count(self):
        from scripts.discover_catalog_portals import standard_sitemap_candidates

        return len(set(standard_sitemap_candidates("https://sport.example.com", "example.com")))

    def test_sitemap_walk_stops_once_the_budget_is_met(self):
        budget = 5000
        result, calls = self._run(
            lambda sitemap, **kwargs: self._articles("full", budget), max_urls=budget
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["portal_stats"]["example"]["discovered"], budget)

    def test_every_candidate_is_walked_when_the_budget_is_not_met(self):
        _, calls = self._run(lambda sitemap, **kwargs: self._articles("few", 1))

        self.assertEqual(len(calls), self._candidate_count())

    def test_discovered_articles_are_queued_in_the_frontier(self):
        counter = {"n": 0}

        def walk(sitemap, **kwargs):
            counter["n"] += 1
            return self._articles(f"c{counter['n']}", 3)

        result, _ = self._run(walk)
        expected = self._candidate_count() * 3

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_discovered"], expected)
        self.assertEqual(result["total_new_queued"], expected)

    def test_sitemap_failure_is_recorded_and_the_run_continues(self):
        counter = {"n": 0}

        def walk(sitemap, **kwargs):
            counter["n"] += 1
            if counter["n"] == 1:
                raise RuntimeError("boom")
            return self._articles(f"s{counter['n']}", 2)

        with self.assertLogs("catalog_discovery", level="ERROR") as logs:
            result, _ = self._run(walk)

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["total_failures"], 1)
        self.assertEqual(
            result["portal_stats"]["example"]["failures"], ["sitemap:RuntimeError"]
        )
        self.assertEqual(result["total_discovered"], (self._candidate_count() - 1) * 2)
        self.assertTrue(any("sitemap failed for" in line for line in logs.output))

    def test_wp_rest_failure_is_recorded(self):
        fingerprints = [{
            "domain": "example.com",
            "has_wp_json": True,
            "wp_json_endpoint": "https://example.com/wp-json/wp/v2/posts",
        }]

        def wp(**kwargs):
            raise ValueError("bad payload")

        with self.assertLogs("catalog_discovery", level="ERROR"):
            result, _ = self._run(
                lambda sitemap, **kwargs: self._articles("s", 1),
                fingerprints=fingerprints,
                wp_side_effect=wp,
            )

        self.assertEqual(result["total_failures"], 1)
        self.assertEqual(
            result["portal_stats"]["example"]["failures"], ["wp_rest:ValueError"]
        )

    def test_stage_failure_is_logged_at_error(self):
        def walk(sitemap, **kwargs):
            raise RuntimeError("boom")

        with self.assertLogs("catalog_discovery", level="ERROR") as logs:
            result, _ = self._run(walk)

        self.assertTrue(any("sitemap failed for" in line for line in logs.output))
        self.assertGreater(result["total_failures"], 0)


if __name__ == "__main__":
    unittest.main()
