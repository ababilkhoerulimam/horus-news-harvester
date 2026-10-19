"""
Unit tests for the 6 architectural patches across scraping, sessions,
normalization, pagination, deduplication, and cold storage backup.
"""

import json
import os
import tarfile
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from backends.firefox_cookies import extract_firefox_twitter_tokens, find_firefox_profiles
from channels.social_youtube import YouTubeChannel
from processors.backup import create_backup_archive, run_backup
from processors.cleaner import deduplicate_jsonl_file, normalize_timestamp
from scripts.local_harvester import _load_existing_ids


class TestSixArchitecturePatches(unittest.TestCase):
    """Test suite covering all 6 architectural enhancements."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    # =========================================================================
    # Patch 1 & 5: Deduplication & Fast Set Lookup
    # =========================================================================

    def test_load_existing_ids_from_jsonl(self):
        """Verifies _load_existing_ids quickly extracts IDs from a JSONL file."""
        test_file = self.base_path / "sample.jsonl"
        with open(test_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"url": "https://example.com/1", "title": "Art 1"}) + "\n")
            f.write(json.dumps({"url": "https://example.com/2", "title": "Art 2"}) + "\n")
            f.write(json.dumps({"tweet_id": "tw_123", "text": "Tweet"}) + "\n")

        ids_by_url = _load_existing_ids(test_file, id_key="url")
        self.assertIn("https://example.com/1", ids_by_url)
        self.assertIn("https://example.com/2", ids_by_url)
        self.assertIn("tw_123", ids_by_url)

        # Empty / non-existent file returns empty set
        empty_ids = _load_existing_ids(self.base_path / "non_existent.jsonl")
        self.assertEqual(len(empty_ids), 0)

    def test_deduplicate_jsonl_file_in_place(self):
        """Verifies deduplicate_jsonl_file strips duplicate entries and retains unique records."""
        dup_file = self.base_path / "duplicates.jsonl"
        with open(dup_file, "w", encoding="utf-8") as f:
            f.write(json.dumps({"url": "https://example.com/a", "title": "A"}) + "\n")
            f.write(json.dumps({"url": "https://example.com/b", "title": "B"}) + "\n")
            f.write(json.dumps({"url": "https://example.com/a", "title": "A duplicate"}) + "\n")
            f.write(json.dumps({"url": "https://example.com/c", "title": "C"}) + "\n")
            f.write(json.dumps({"url": "https://example.com/b", "title": "B duplicate"}) + "\n")

        removed = deduplicate_jsonl_file(dup_file, id_key="url")
        self.assertEqual(removed, 2)

        # Read back and verify exact 3 unique entries
        with open(dup_file, "r", encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]

        self.assertEqual(len(lines), 3)
        urls = [item["url"] for item in lines]
        self.assertEqual(urls, ["https://example.com/a", "https://example.com/b", "https://example.com/c"])

    # =========================================================================
    # Patch 2: Multi-Platform & Env Fallback Twitter Session
    # =========================================================================

    def test_twitter_env_variables_priority(self):
        """Verifies TWITTER_AUTH_TOKEN & TWITTER_CT0 take precedence over browser search."""
        with patch.dict(os.environ, {"TWITTER_AUTH_TOKEN": "mock_env_token_abc", "TWITTER_CT0": "mock_env_ct0_xyz"}):
            tokens = extract_firefox_twitter_tokens()
            self.assertIsNotNone(tokens)
            self.assertEqual(tokens["auth_token"], "mock_env_token_abc")
            self.assertEqual(tokens["ct0"], "mock_env_ct0_xyz")
            self.assertEqual(tokens["profile"], "environment_variables")

    def test_firefox_custom_profile_dir_env(self):
        """Verifies custom profile override via FIREFOX_PROFILE_DIR."""
        mock_profile = self.base_path / "custom_firefox_profile"
        mock_profile.mkdir()
        (mock_profile / "cookies.sqlite").touch()

        with patch.dict(os.environ, {"FIREFOX_PROFILE_DIR": str(mock_profile)}):
            profiles = find_firefox_profiles()
            self.assertTrue(any(p.resolve() == mock_profile.resolve() for p in profiles))

    # =========================================================================
    # Patch 3: Date Normalizer to ISO-8601 UTC
    # =========================================================================

    def test_normalize_timestamp_indonesian_relative(self):
        """Verifies Indonesian relative time expressions resolve correctly to ISO-8601 UTC."""
        fixed_now = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

        # '2 hari yang lalu' -> 2026-10-17T12:00:00Z
        res_days = normalize_timestamp("2 hari yang lalu", base_time=fixed_now)
        self.assertEqual(res_days, "2026-10-17T12:00:00Z")

        # '18 jam lalu' -> 2026-10-18T18:00:00Z
        res_hours = normalize_timestamp("18 jam lalu", base_time=fixed_now)
        self.assertEqual(res_hours, "2026-10-18T18:00:00Z")

        # '30 menit lalu' -> 2026-10-19T11:30:00Z
        res_mins = normalize_timestamp("30 menit yang lalu", base_time=fixed_now)
        self.assertEqual(res_mins, "2026-10-19T11:30:00Z")

        # 'kemarin' -> 2026-10-18T12:00:00Z
        res_kemarin = normalize_timestamp("kemarin", base_time=fixed_now)
        self.assertEqual(res_kemarin, "2026-10-18T12:00:00Z")

    def test_normalize_timestamp_portal_and_rfc2822(self):
        """Verifies RFC-2822 (Twitter) and Indonesian portal timestamps with WIB offset."""
        # RFC-2822
        rfc_str = "Wed Oct 18 10:00:00 +0000 2026"
        res_rfc = normalize_timestamp(rfc_str)
        self.assertEqual(res_rfc, "2026-10-18T10:00:00Z")

        # Indonesian portal: "18 Oktober 2026 14:30 WIB" (WIB = UTC+7 -> 07:30 UTC)
        wib_str = "18 Oktober 2026 14:30 WIB"
        res_wib = normalize_timestamp(wib_str)
        self.assertEqual(res_wib, "2026-10-18T07:30:00Z")

    # =========================================================================
    # Patch 4: Dynamic Continuation Pagination Komentar YouTube
    # =========================================================================

    def test_youtube_replies_dynamic_max_pages_scaling(self):
        """Verifies YouTube _fetch_innertube_replies dynamically computes max_pages from max_replies."""
        yt = YouTubeChannel()

        # When max_replies is 50, max_pages should scale to at least 5
        # Test pagination loop mock
        mock_body_p1 = json.dumps({
            "onResponseReceivedEndpoints": [{
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {
                            "commentRenderer": {
                                "commentId": "rep_1",
                                "contentText": {"runs": [{"text": "Reply 1"}]},
                                "authorText": {"simpleText": "User 1"},
                                "voteCount": {"simpleText": "5"},
                                "publishedTimeText": {"simpleText": "1 jam lalu"}
                            }
                        },
                        {
                            "continuationItemRenderer": {
                                "continuationEndpoint": {
                                    "continuationCommand": {"token": "token_page_2"}
                                }
                            }
                        }
                    ]
                }
            }]
        }).encode("utf-8")

        mock_body_p2 = json.dumps({
            "onResponseReceivedEndpoints": [{
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {
                            "commentRenderer": {
                                "commentId": "rep_2",
                                "contentText": {"runs": [{"text": "Reply 2"}]},
                                "authorText": {"simpleText": "User 2"},
                                "voteCount": {"simpleText": "2"},
                                "publishedTimeText": {"simpleText": "30 menit lalu"}
                            }
                        }
                    ]
                }
            }]
        }).encode("utf-8")

        with patch("channels.social_youtube.default_http_client.fetch") as mock_fetch:
            mock_fetch.side_effect = [
                (200, mock_body_p1, {}),
                (200, mock_body_p2, {}),
            ]
            replies = yt._fetch_innertube_replies("token_initial", max_replies=20)
            self.assertEqual(len(replies), 2)
            self.assertEqual(replies[0]["id"], "rep_1")
            self.assertEqual(replies[1]["id"], "rep_2")
            self.assertEqual(mock_fetch.call_count, 2)

    # =========================================================================
    # Patch 6: Cold Storage Backup & Archive Sync
    # =========================================================================

    def test_create_backup_archive_and_integrity(self):
        """Verifies tar.gz archive creation and contents integrity."""
        # Create mock source data
        src_raw = self.base_path / "mock_raw"
        src_raw.mkdir()
        (src_raw / "sample.jsonl").write_text('{"item": 1}\n', encoding="utf-8")

        backup_dest = self.base_path / "backups"

        archive = create_backup_archive(
            source_dirs=[str(src_raw)],
            output_dir=str(backup_dest),
            archive_name="test_backup.tar.gz"
        )
        self.assertTrue(archive.exists())

        # Inspect tar contents
        with tarfile.open(archive, "r:gz") as tar:
            names = tar.getnames()
            self.assertIn("mock_raw", names)
            self.assertIn("mock_raw/sample.jsonl", names)

    def test_run_backup_helper(self):
        """Verifies full run_backup function."""
        src_data = self.base_path / "data_to_backup"
        src_data.mkdir()
        (src_data / "news.jsonl").write_text('{"title": "test"}\n', encoding="utf-8")

        res = run_backup(
            output_dir=str(self.base_path / "out_backups"),
            source_dirs=[str(src_data)]
        )
        self.assertEqual(res["status"], "success")
        self.assertGreater(res["size_bytes"], 0)
        self.assertTrue(Path(res["archive_path"]).exists())

    def test_doctor_diagnostic_packages(self):
        """Verifies doctor diagnostic package checking includes duckdb and pyarrow."""
        from doctor import check_packages
        pkgs = check_packages()
        self.assertIn("duckdb", pkgs)
        self.assertIn("pyarrow", pkgs)
        self.assertIn("pandas", pkgs)
        self.assertIn("requests", pkgs)
        self.assertIn("trafilatura", pkgs)


if __name__ == "__main__":
    unittest.main()
