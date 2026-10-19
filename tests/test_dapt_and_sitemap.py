"""
Unit tests for Trafilatura text extraction, XML sitemap backfilling,
and NusaBERT DAPT corpus preparation.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from channels.portal_news import extract_clean_article_text
from doctor import check_packages
from scripts.prepare_dapt_corpus import (
    build_dapt_corpus,
    format_corpus_summary,
    sanitize_dapt_text,
)
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


if __name__ == "__main__":
    unittest.main()
