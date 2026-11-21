"""Tests for the offline corpus normalizer."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.normalize_corpus import (
    main,
    normalize_content,
    normalize_record,
)

ARTICLE = (
    "Jakarta - Pelatih Persib Bandung menyebut anak asuhnya bermain disiplin. "
    "Ia menilai laga berjalan sesuai rencana dan menegaskan rotasi tetap dilakukan."
)
ENVELOPE = "URL Source: https://a.example.com/x\nMarkdown Content:\n" + ARTICLE
CAPTION = "Pemain Persib berlatih di stadion jelang laga. (BP/Antara)"


class NormalizeContentTests(unittest.TestCase):
    def test_unescape_entities(self):
        self.assertEqual(normalize_content("Gol &amp; kartu merah"), "Gol & kartu merah")

    def test_nbsp_becomes_a_space(self):
        self.assertEqual(normalize_content("kencangnya.&nbsp;"), "kencangnya.")

    def test_dateline_is_removed(self):
        self.assertFalse(normalize_content("Jakarta - " + ARTICLE).startswith("Jakarta"))

    def test_baca_juga_without_a_colon_is_removed(self):
        text = "Baca juga Tanpa Titik Dua di Sini\n\n" + ARTICLE

        self.assertNotIn("Baca juga", normalize_content(text))

    def test_content_is_a_fixed_point(self):
        once = normalize_content("Jakarta - Gol &amp; kartu.\n\nBaca juga Tanpa Titik Dua\n\nIsi.")

        self.assertEqual(normalize_content(once), once)


class NormalizeRecordTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "url": "https://a.example.com/x",
            "title": "Judul Asli",
            "published_date": "2026-01-01",
            "archive_date": "2026-01-01",
            "content": "Jakarta - Gol &amp; kartu merah. " + ARTICLE,
            "source": "a.example.com",
            "backend": "curl_impersonate",
            "quality": "low",
            "content_chars": 9999,
            "harvested_at": "2026-01-01T00:00:00Z",
        }

    def test_record_keys_are_unchanged(self):
        updated, _ = normalize_record(self.record)

        self.assertEqual(set(updated), set(self.record))

    def test_missing_quality_and_content_chars_are_added(self):
        record = {k: v for k, v in self.record.items() if k not in ("quality", "content_chars")}

        updated, _ = normalize_record(record)

        self.assertEqual(set(updated), set(self.record))

    def test_nested_entities_need_a_second_run(self):
        once = normalize_content("Gol &amp;nbsp; kartu")

        self.assertIn("&nbsp;", once)
        self.assertNotIn("&nbsp;", normalize_content(once))

    def test_url_and_backend_are_untouched(self):
        updated, _ = normalize_record(self.record)

        self.assertEqual(updated["url"], self.record["url"])
        self.assertEqual(updated["backend"], self.record["backend"])

    def test_title_is_untouched(self):
        updated, _ = normalize_record(self.record)

        self.assertEqual(updated["title"], "Judul Asli")

    def test_quality_is_reclassified(self):
        updated, flags = normalize_record(self.record)
        self.assertEqual(updated["quality"], "ok")
        self.assertTrue(flags["relabelled"])

        envelope, _ = normalize_record(dict(self.record, content=ENVELOPE))
        self.assertEqual(envelope["quality"], "reject")

        caption, _ = normalize_record(dict(self.record, content=CAPTION))
        self.assertEqual(caption["quality"], "low")

    def test_content_chars_are_recomputed(self):
        updated, _ = normalize_record(self.record)

        self.assertEqual(updated["content_chars"], len(updated["content"]))
        self.assertNotEqual(updated["content_chars"], 9999)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "corpus.jsonl"
        self.records = [
            dict(
                url="https://a.example.com/1",
                title="Satu",
                content="Jakarta - Gol &amp; kartu merah. " + ARTICLE,
                backend="curl_impersonate",
                quality="low",
                content_chars=1,
            ),
            dict(
                url="https://a.example.com/2",
                title="Dua",
                content=ENVELOPE,
                backend="curl_impersonate",
                quality="ok",
                content_chars=1,
            ),
            dict(
                url="https://a.example.com/3",
                title="Tiga",
                content=CAPTION,
                backend="direct_http",
                quality="ok",
                content_chars=1,
            ),
        ]
        with open(self.path, "w", encoding="utf-8") as handle:
            for record in self.records:
                handle.write(json.dumps(record) + "\n")
        self.original = self.path.read_bytes()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _run(self, *flags):
        buffer = io.StringIO()
        with patch.object(sys, "argv", ["normalize_corpus.py", "--path", str(self.path), *flags]):
            with redirect_stdout(buffer):
                code = main()
        return code, buffer.getvalue()

    def test_dry_run_writes_nothing(self):
        code, output = self._run("--dry-run", "--report")

        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertFalse(self.path.with_suffix(".jsonl.bak").exists())
        self.assertIn("records=3", output)

    def test_apply_keeps_a_backup_first(self):
        code, _ = self._run("--apply")

        self.assertEqual(code, 0)
        self.assertEqual(self.path.with_suffix(".jsonl.bak").read_bytes(), self.original)

    def test_apply_is_idempotent(self):
        self._run("--apply")
        once = self.path.read_bytes()
        code, output = self._run("--apply", "--report")

        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), once)
        self.assertIn("changed=0", output)

    def test_apply_refuses_without_disk_space(self):
        with patch("scripts.normalize_corpus.shutil.disk_usage") as usage:
            usage.return_value.free = 1
            code, output = self._run("--apply")

        self.assertEqual(code, 1)
        self.assertIn("refused", output)
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_report_matches_the_corpus(self):
        code, output = self._run("--dry-run", "--report")

        self.assertEqual(code, 0)
        self.assertIn("changed=1", output)
        self.assertIn("emptied=0", output)
        self.assertIn("label=reject | records=1", output)
        self.assertIn("label=low | records=1", output)


class DependencyTests(unittest.TestCase):
    def test_no_network_imports(self):
        source = (
            Path(__file__).resolve().parent.parent / "scripts" / "normalize_corpus.py"
        ).read_text(encoding="utf-8")

        for forbidden in ("backends.", "channels.", "http_client"):
            with self.subTest(module=forbidden):
                self.assertNotIn("import " + forbidden, source)
                self.assertNotIn("from " + forbidden, source)


if __name__ == "__main__":
    unittest.main()
