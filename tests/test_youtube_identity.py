"""
YouTube identity: the headers a logged-in web client sends, and the User-Agent
that has to match the TLS fingerprint curl_cffi already pins.
"""

import hashlib
import json
import unittest
from unittest.mock import patch

from channels import innertube
from processors.http_client import DEFAULT_USER_AGENTS, ResilientHttpClient


def _expected_hash(timestamp, sapisid):
    digest = hashlib.sha1(f"{timestamp} {sapisid} https://www.youtube.com".encode("utf-8")).hexdigest()
    return f"SAPISIDHASH {timestamp}_{digest}"


class IdentityHeaderTests(unittest.TestCase):
    def tearDown(self):
        innertube.configure_cookies({})
        innertube.configure_visitor_data("")

    def test_no_session_means_no_identity_headers(self):
        innertube.configure_cookies({})
        self.assertEqual(innertube.identity_headers(), {})

    def test_origin_and_auth_user_are_sent_with_a_session(self):
        innertube.configure_cookies({"SID": "abc"})
        headers = innertube.identity_headers()
        self.assertEqual(headers["X-Origin"], "https://www.youtube.com")
        self.assertEqual(headers["X-Goog-AuthUser"], "0")
        self.assertNotIn("Authorization", headers)

    def test_sapisid_hash_matches_the_documented_recipe(self):
        innertube.configure_cookies({"SAPISID": "secret-token"})
        self.assertEqual(innertube.sapisid_hash(timestamp=1700000000), _expected_hash(1700000000, "secret-token"))

    def test_secure_3papisid_is_accepted_when_sapisid_is_absent(self):
        innertube.configure_cookies({"__Secure-3PAPISID": "token-3p"})
        self.assertEqual(innertube.sapisid_hash(timestamp=42), _expected_hash(42, "token-3p"))

    def test_authorization_is_attached_when_a_token_exists(self):
        innertube.configure_cookies({"SAPISID": "secret-token"})
        headers = innertube.identity_headers()
        self.assertTrue(headers["Authorization"].startswith("SAPISIDHASH "))

    def test_visitor_id_header_requires_visitor_data(self):
        innertube.configure_cookies({"SAPISID": "secret-token"})
        self.assertNotIn("X-Goog-Visitor-Id", innertube.identity_headers())
        innertube.configure_visitor_data("visitor-123")
        self.assertEqual(innertube.identity_headers()["X-Goog-Visitor-Id"], "visitor-123")


class PostInnertubeIdentityTests(unittest.TestCase):
    def tearDown(self):
        innertube.configure_cookies({})
        innertube.configure_visitor_data("")
        innertube.configure_pacing(0.0)

    def _post(self):
        with patch("channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})) as fetch:
            innertube.post_innertube("https://example.com/youtubei/v1/next", {"context": {}})
        return fetch.call_args

    def test_anonymous_request_carries_no_cookie_or_identity(self):
        innertube.configure_cookies({})
        call = self._post()
        headers = call.kwargs["headers"]
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("X-Origin", headers)
        body = json.loads(call.kwargs["data"].decode("utf-8"))
        self.assertNotIn("visitorData", body["context"]["client"])

    def test_session_request_carries_cookie_and_identity(self):
        innertube.configure_cookies({"SAPISID": "secret-token", "SID": "sid"})
        innertube.configure_visitor_data("visitor-123")
        call = self._post()
        headers = call.kwargs["headers"]
        self.assertIn("SAPISID=secret-token", headers["Cookie"])
        self.assertEqual(headers["X-Origin"], "https://www.youtube.com")
        self.assertEqual(headers["X-Goog-Visitor-Id"], "visitor-123")
        self.assertIn("Authorization", headers)
        body = json.loads(call.kwargs["data"].decode("utf-8"))
        self.assertEqual(body["context"]["client"]["visitorData"], "visitor-123")

    def test_refresh_captures_visitor_data_from_the_page(self):
        page = b'<script>{"INNERTUBE_CLIENT_VERSION":"2.20260101.00.00","VISITOR_DATA":"visitor-xyz"}</script>'
        with patch("channels.innertube.default_http_client.fetch", return_value=(200, page, {})):
            innertube.refresh_client_version()
        self.assertEqual(innertube.visitor_data(), "visitor-xyz")
        self.assertEqual(innertube.client_version(), "2.20260101.00.00")


class PinnedUserAgentTests(unittest.TestCase):
    def test_rotation_is_the_default(self):
        client = ResilientHttpClient()
        self.assertIsNone(client.pinned_user_agent())
        self.assertIn(client.get_random_user_agent(), DEFAULT_USER_AGENTS)

    def test_pinning_repeats_one_agent(self):
        client = ResilientHttpClient()
        client.set_user_agent("Mozilla/5.0 pinned")
        self.assertEqual({client.get_random_user_agent() for _ in range(20)}, {"Mozilla/5.0 pinned"})

    def test_pin_can_be_cleared(self):
        client = ResilientHttpClient()
        client.set_user_agent("Mozilla/5.0 pinned")
        self.assertIsNone(client.set_user_agent(None))
        self.assertIn(client.get_random_user_agent(), DEFAULT_USER_AGENTS)

    def test_blank_pin_is_treated_as_no_pin(self):
        client = ResilientHttpClient()
        self.assertIsNone(client.set_user_agent("   "))

    def test_fetch_uses_the_pinned_agent_on_retries(self):
        client = ResilientHttpClient()
        client.set_user_agent("Mozilla/5.0 pinned")
        seen = []

        def fake_send(url, data, headers, timeout, proxy):
            seen.append(headers["User-Agent"])
            return 429, b"", {}

        with patch.object(client, "_send_curl", side_effect=fake_send), \
             patch.object(client, "_resolve_transport", return_value=True), \
             patch("processors.http_client.time.sleep", return_value=None):
            client.fetch("https://example.com", transport="curl", max_retries=2)
        self.assertEqual(len(seen), 3)  # first attempt plus two retries
        self.assertEqual(set(seen), {"Mozilla/5.0 pinned"})


if __name__ == "__main__":
    unittest.main()
