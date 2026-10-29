"""
Unit tests for the pluggable HTTP transport, the 429 backoff formula, and proxy
error classification.
"""

import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from channels import innertube
from processors import http_client
from processors.http_client import (
    ResilientHttpClient,
    backoff_seconds,
    is_proxy_error,
)


class FakeResponse:
    """Minimal stand-in for a curl_cffi response."""

    def __init__(self, status, content=b"", headers=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class FakeSession:
    """Records requests and replays a queued list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0)


class TestBackoffFormula(unittest.TestCase):
    def test_retry_after_is_honoured(self):
        self.assertEqual(backoff_seconds(1, "12"), 12.0)

    def test_retry_after_is_capped(self):
        self.assertEqual(backoff_seconds(1, "600"), 45.0)

    def test_exponential_growth_is_capped(self):
        self.assertGreaterEqual(backoff_seconds(20, None), 30.0)
        self.assertLessEqual(backoff_seconds(20, None), 31.5)

    def test_second_attempt_doubles(self):
        self.assertGreaterEqual(backoff_seconds(2, None), 10.5)
        self.assertLessEqual(backoff_seconds(2, None), 11.5)

    def test_non_numeric_retry_after_falls_back_to_exponential(self):
        self.assertGreaterEqual(backoff_seconds(1, "later"), 5.5)
        self.assertLessEqual(backoff_seconds(1, "later"), 6.5)


class TestProxyErrorClassification(unittest.TestCase):
    def test_connection_refused_is_proxy_error(self):
        self.assertTrue(is_proxy_error(OSError("Connection refused")))

    def test_unresolvable_proxy_is_proxy_error(self):
        self.assertTrue(is_proxy_error(OSError("Could not resolve proxy: bad.host")))

    def test_target_side_tls_failure_is_not_proxy_error(self):
        self.assertFalse(is_proxy_error(OSError("SSL: CERTIFICATE_VERIFY_FAILED")))

    def test_http_403_is_not_proxy_error(self):
        self.assertFalse(is_proxy_error(OSError("HTTP Error 403: Forbidden")))


class TestTransportSelection(unittest.TestCase):
    def setUp(self):
        self.client = ResilientHttpClient(enable_jitter=False)

    def _ok_response(self, body=b"ok"):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = body
        mock_resp.headers = {"Content-Type": "application/json"}
        mock_resp.__enter__.return_value = mock_resp
        return mock_resp

    def test_default_transport_uses_urllib(self):
        with patch("urllib.request.urlopen", return_value=self._ok_response()) as mock_urlopen:
            status, body, _ = self.client.fetch("https://example.com")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_curl_transport_uses_pooled_session(self):
        session = FakeSession([FakeResponse(200, b'{"a":1}', {"Content-Type": "application/json"})])
        with patch.object(self.client, "_curl_session", return_value=session), patch(
            "urllib.request.urlopen"
        ) as mock_urlopen:
            status, body, _ = self.client.fetch(
                "https://example.com/api", data=b"{}", transport="curl"
            )
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"a":1}')
        self.assertEqual(mock_urlopen.call_count, 0)
        self.assertEqual(session.calls[0]["method"], "POST")

    def test_auto_uses_curl_when_library_present(self):
        session = FakeSession([FakeResponse(200, b"auto")])
        with patch.object(http_client, "HAS_CURL_CFFI", True), patch.object(
            self.client, "_curl_session", return_value=session
        ):
            status, body, _ = self.client.fetch("https://example.com", transport="auto")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"auto")
        self.assertEqual(len(session.calls), 1)

    def test_auto_falls_back_when_library_missing(self):
        with patch.object(http_client, "HAS_CURL_CFFI", False), patch(
            "urllib.request.urlopen", return_value=self._ok_response()
        ) as mock_urlopen:
            status, _, _ = self.client.fetch("https://example.com", transport="auto")
        self.assertEqual(status, 200)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_forced_curl_falls_back_when_library_missing(self):
        with patch.object(http_client, "HAS_CURL_CFFI", False), patch(
            "urllib.request.urlopen", return_value=self._ok_response()
        ) as mock_urlopen:
            status, _, _ = self.client.fetch("https://example.com", transport="curl")
        self.assertEqual(status, 200)
        self.assertEqual(mock_urlopen.call_count, 1)

    def test_curl_429_sleeps_then_retries(self):
        session = FakeSession(
            [
                FakeResponse(429, b"", {"Retry-After": "7"}),
                FakeResponse(200, b"second"),
            ]
        )
        with patch.object(self.client, "_curl_session", return_value=session), patch(
            "time.sleep"
        ) as mock_sleep:
            status, body, _ = self.client.fetch("https://example.com", transport="curl", max_retries=1)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"second")
        self.assertEqual(len(session.calls), 2)
        mock_sleep.assert_called_once_with(7.0)

    def test_curl_client_error_is_returned_without_retry(self):
        session = FakeSession([FakeResponse(404, b"")])
        with patch.object(self.client, "_curl_session", return_value=session):
            status, body, _ = self.client.fetch("https://example.com", transport="curl", max_retries=2)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")
        self.assertEqual(len(session.calls), 1)

    def test_curl_connection_refused_marks_proxy_failed(self):
        session = MagicMock()
        session.request.side_effect = OSError("Connection refused")
        self.client._single_proxy = "http://127.0.0.1:9"
        with patch.object(self.client, "_curl_session", return_value=session):
            status, _, _ = self.client.fetch("https://example.com", transport="curl", max_retries=0)
        self.assertEqual(status, 500)
        self.assertIn("http://127.0.0.1:9", self.client._failed_proxies)

    def test_curl_tls_error_does_not_mark_proxy_failed(self):
        session = MagicMock()
        session.request.side_effect = OSError("SSL: CERTIFICATE_VERIFY_FAILED")
        self.client._single_proxy = "http://127.0.0.1:9"
        with patch.object(self.client, "_curl_session", return_value=session):
            status, _, _ = self.client.fetch("https://example.com", transport="curl", max_retries=0)
        self.assertEqual(status, 500)
        self.assertNotIn("http://127.0.0.1:9", self.client._failed_proxies)

    def _fetch_through_failing_opener(self, error):
        self.client._single_proxy = "http://127.0.0.1:9"
        opener = MagicMock()
        opener.open.side_effect = error
        with patch("urllib.request.build_opener", return_value=opener):
            return self.client.fetch("https://example.com", max_retries=0)

    def test_urllib_connection_refused_marks_proxy_failed(self):
        status, _, _ = self._fetch_through_failing_opener(urllib.error.URLError("Connection refused"))
        self.assertEqual(status, 500)
        self.assertIn("http://127.0.0.1:9", self.client._failed_proxies)

    def test_urllib_tls_error_does_not_mark_proxy_failed(self):
        status, _, _ = self._fetch_through_failing_opener(
            urllib.error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")
        )
        self.assertEqual(status, 500)
        self.assertNotIn("http://127.0.0.1:9", self.client._failed_proxies)


class TestInnertubeTransportSwitch(unittest.TestCase):
    def tearDown(self):
        innertube.configure_transport("auto")

    def test_default_is_auto(self):
        self.assertEqual(innertube.configure_transport("auto"), "auto")

    def test_rejects_unknown_transport(self):
        with self.assertRaises(ValueError):
            innertube.configure_transport("carrier-pigeon")

    def test_post_innertube_forwards_active_transport(self):
        innertube.configure_transport("urllib")
        with patch("channels.innertube.default_http_client.fetch", return_value=(200, b"{}", {})) as mock_fetch:
            innertube.post_innertube("https://example.com/youtubei/v1/next", {"context": {}})
        self.assertEqual(mock_fetch.call_args.kwargs["transport"], "urllib")


if __name__ == "__main__":
    unittest.main()
