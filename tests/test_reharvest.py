"""
Longest-wins merging: the re-harvest selection, the dedupe mode it relies on, and
the exporter upsert that keeps a good body from being overwritten by a stub.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from processors.cleaner import deduplicate_jsonl_file
from processors.exporter import SportsAnalyticsExporter
from scripts.reharvest_low_quality import merge_into, select_targets, write_side_file


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return path


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


class DeduplicateLongestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "news.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_default_keeps_the_first_record(self):
        _write_jsonl(self.path, [
            {"url": "u1", "content": "short"},
            {"url": "u1", "content": "a much longer body"},
        ])
        removed = deduplicate_jsonl_file(self.path)
        self.assertEqual(removed, 1)
        self.assertEqual(_read_jsonl(self.path)[0]["content"], "short")

    def test_longest_mode_keeps_the_better_body(self):
        _write_jsonl(self.path, [
            {"url": "u1", "content": "short"},
            {"url": "u1", "content": "a much longer body"},
        ])
        deduplicate_jsonl_file(self.path, prefer="longest")
        records = _read_jsonl(self.path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["content"], "a much longer body")

    def test_longest_mode_keeps_the_first_when_it_is_already_best(self):
        _write_jsonl(self.path, [
            {"url": "u1", "content": "the longest body here"},
            {"url": "u1", "content": "tiny"},
        ])
        deduplicate_jsonl_file(self.path, prefer="longest")
        self.assertEqual(_read_jsonl(self.path)[0]["content"], "the longest body here")

    def test_longest_mode_does_not_reorder_other_records(self):
        _write_jsonl(self.path, [
            {"url": "u1", "content": "short"},
            {"url": "u2", "content": "other"},
            {"url": "u1", "content": "a much longer body"},
        ])
        deduplicate_jsonl_file(self.path, prefer="longest")
        self.assertEqual([record["url"] for record in _read_jsonl(self.path)], ["u1", "u2"])

    def test_unknown_prefer_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            deduplicate_jsonl_file(self.path, prefer="newest")


class SelectTargetsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "news.jsonl"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_tagged_low_records_are_selected(self):
        _write_jsonl(self.path, [
            {"url": "https://a.example.com/1", "content": "x" * 5000, "quality": "low"},
        ])
        self.assertEqual(len(select_targets([self.path])), 1)

    def test_untagged_short_records_are_selected(self):
        _write_jsonl(self.path, [
            {"url": "https://a.example.com/1", "content": "x" * 300},
        ])
        self.assertEqual(len(select_targets([self.path])), 1)

    def test_untagged_long_records_are_skipped(self):
        _write_jsonl(self.path, [
            {"url": "https://a.example.com/1", "content": "x" * 5000},
        ])
        self.assertEqual(select_targets([self.path]), [])

    def test_healthy_tagged_records_are_skipped(self):
        _write_jsonl(self.path, [
            {"url": "https://a.example.com/1", "content": "x" * 5000, "quality": "ok"},
        ])
        self.assertEqual(select_targets([self.path]), [])

    def test_domain_filter_and_limit_apply(self):
        _write_jsonl(self.path, [
            {"url": "https://detik.com/1", "content": "x" * 100},
            {"url": "https://kompas.com/2", "content": "x" * 100},
            {"url": "https://detik.com/3", "content": "x" * 100},
        ])
        selected = select_targets([self.path], domain="detik.com")
        self.assertEqual([record["url"] for record in selected], ["https://detik.com/1", "https://detik.com/3"])
        self.assertEqual(len(select_targets([self.path], limit=2)), 2)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = _write_jsonl(self.root / "news.jsonl", [
            {"url": "u1", "content": "short", "quality": "low"},
            {"url": "u2", "content": "already good", "quality": "ok"},
        ])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_merge_keeps_the_longer_body_and_backs_up(self):
        side = write_side_file([{"url": "u1", "content": "the improved body", "quality": "ok"}], self.path)
        merge_into(self.path, side)

        records = {record["url"]: record for record in _read_jsonl(self.path)}
        self.assertEqual(len(records), 2)
        self.assertEqual(records["u1"]["content"], "the improved body")
        self.assertEqual(records["u2"]["content"], "already good")
        self.assertTrue((self.root / "news.jsonl.bak").exists())

    def test_merge_does_not_shorten_a_good_record(self):
        side = write_side_file([{"url": "u2", "content": "tiny", "quality": "low"}], self.path)
        merge_into(self.path, side)
        records = {record["url"]: record for record in _read_jsonl(self.path)}
        self.assertEqual(records["u2"]["content"], "already good")


class ExporterLongestWinsTests(unittest.TestCase):
    """The export must not let a later stub overwrite an earlier good body."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.raw = self.root / "raw"
        self.analytics = self.root / "analytics"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_longer_body_survives_regardless_of_file_order(self):
        # a_long sorts first, z_short sorts last: INSERT OR REPLACE would keep the stub.
        _write_jsonl(self.raw / "news" / "a_long.jsonl", [
            {"url": "https://a.example.com/1", "title": "T", "content": "x" * 5000, "sport": "football"},
        ])
        _write_jsonl(self.raw / "news" / "z_short.jsonl", [
            {"url": "https://a.example.com/1", "title": "T", "content": "stub", "sport": "football"},
        ])

        exporter = SportsAnalyticsExporter(raw_data_dir=str(self.raw), analytics_dir=str(self.analytics))
        exporter.export_sqlite()

        db = sqlite3.connect(self.analytics / "sports_analytics.db")
        try:
            content = db.execute("SELECT content FROM articles WHERE url = ?", ("https://a.example.com/1",)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(len(content), 5000)

    def test_shorter_body_still_inserts_when_url_is_new(self):
        _write_jsonl(self.raw / "news" / "a.jsonl", [
            {"url": "https://a.example.com/new", "title": "T", "content": "stub", "sport": "football"},
        ])
        exporter = SportsAnalyticsExporter(raw_data_dir=str(self.raw), analytics_dir=str(self.analytics))
        exporter.export_sqlite()

        db = sqlite3.connect(self.analytics / "sports_analytics.db")
        try:
            count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        finally:
            db.close()
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
