"""
Cookie extraction from a Firefox profile, the per-domain session store, and the
InnerTube cookie header.
"""

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backends.firefox_cookies import extract_cookies, extract_firefox_twitter_tokens
from channels import innertube
from processors import session_store


def _make_profile(root: Path, rows, name="default-release"):
    profile = root / name
    profile.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(profile / "cookies.sqlite"))
    conn.execute("CREATE TABLE moz_cookies (host TEXT, name TEXT, value TEXT)")
    conn.executemany("INSERT INTO moz_cookies VALUES (?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return profile


class ExtractCookiesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.profile = _make_profile(
            self.root,
            [
                ("youtube.com", "SID", "sid-value"),
                (".youtube.com", "PREF", "pref-value"),
                ("www.youtube.com", "VISITOR_INFO1_LIVE", "visitor-value"),
                ("x.com", "auth_token", "twitter-token"),
                ("x.com", "ct0", "csrf-token"),
                ("example.com", "sessionid", "nope"),
            ],
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_filters_by_domain(self):
        cookies = extract_cookies(["youtube.com"], profile=self.profile)
        self.assertEqual(set(cookies), {"youtube.com", ".youtube.com", "www.youtube.com"})
        self.assertNotIn("example.com", cookies)

    def test_suffix_match_covers_subdomains(self):
        cookies = extract_cookies(["youtube.com"], profile=self.profile)
        self.assertEqual(cookies["www.youtube.com"]["VISITOR_INFO1_LIVE"], "visitor-value")
        self.assertEqual(cookies[".youtube.com"]["PREF"], "pref-value")

    def test_filters_by_name(self):
        cookies = extract_cookies(["youtube.com"], names=["PREF"], profile=self.profile)
        self.assertEqual(cookies, {".youtube.com": {"PREF": "pref-value"}})

    def test_multiple_domains(self):
        cookies = extract_cookies(["youtube.com", "x.com"], profile=self.profile)
        self.assertIn("x.com", cookies)
        self.assertIn("youtube.com", cookies)

    def test_empty_domain_list_returns_empty(self):
        self.assertEqual(extract_cookies([], profile=self.profile), {})

    def test_profile_without_database_returns_empty(self):
        empty = self.root / "empty"
        empty.mkdir()
        self.assertEqual(extract_cookies(["youtube.com"], profile=empty), {})

    def test_twitter_wrapper_keeps_its_contract(self):
        env = {k: v for k, v in os.environ.items() if k not in ("TWITTER_AUTH_TOKEN", "TWITTER_CT0")}
        env["FIREFOX_PROFILE_DIR"] = str(self.profile)
        with patch.dict(os.environ, env, clear=True):
            tokens = extract_firefox_twitter_tokens()
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens["auth_token"], "twitter-token")
        self.assertEqual(tokens["ct0"], "csrf-token")
        self.assertEqual(tokens["profile"], "default-release")
        self.assertIn("extracted_at", tokens)


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp_dir.name) / "sessions"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_round_trip(self):
        self.assertTrue(session_store.save("youtube.com", {"SID": "abc"}, self.directory))
        self.assertEqual(session_store.load("youtube.com", self.directory), {"SID": "abc"})

    def test_missing_domain_returns_none(self):
        self.assertIsNone(session_store.load("nope.com", self.directory))

    def test_empty_cookies_are_not_saved(self):
        self.assertFalse(session_store.save("youtube.com", {}, self.directory))

    def test_clear_removes_file(self):
        session_store.save("youtube.com", {"SID": "abc"}, self.directory)
        self.assertTrue(session_store.clear("youtube.com", self.directory))
        self.assertIsNone(session_store.load("youtube.com", self.directory))

    def test_clear_on_absent_file_is_false(self):
        self.assertFalse(session_store.clear("nope.com", self.directory))

    def test_user_agent_is_stored_next_to_the_cookies(self):
        session_store.save(
            "youtube.com", {"SID": "abc"}, self.directory, user_agent="Mozilla/5.0 captured"
        )
        self.assertEqual(session_store.load_user_agent("youtube.com", self.directory), "Mozilla/5.0 captured")
        self.assertEqual(session_store.load("youtube.com", self.directory), {"SID": "abc"})

    def test_user_agent_is_absent_when_not_captured(self):
        session_store.save("youtube.com", {"SID": "abc"}, self.directory)
        self.assertIsNone(session_store.load_user_agent("youtube.com", self.directory))


class InnertubeCookieHeaderTests(unittest.TestCase):
    def tearDown(self):
        innertube.configure_cookies(None)

    def test_no_cookie_header_by_default(self):
        innertube.configure_cookies(None)
        with patch("channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})) as mock_fetch:
            innertube.post_innertube("https://example.com/youtubei/v1/next", {"context": {}})
        self.assertNotIn("Cookie", mock_fetch.call_args.kwargs["headers"])

    def test_cookie_header_is_sent_when_configured(self):
        innertube.configure_cookies({"SID": "abc", "PREF": "id"})
        with patch("channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})) as mock_fetch:
            innertube.post_innertube("https://example.com/youtubei/v1/next", {"context": {}})
        self.assertEqual(mock_fetch.call_args.kwargs["headers"]["Cookie"], "SID=abc; PREF=id")


if __name__ == "__main__":
    unittest.main()
