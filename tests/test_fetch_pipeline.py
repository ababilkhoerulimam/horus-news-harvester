"""Tests for the shared extraction ladder and record shapes."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from processors.content_shape import ARTICLE, CHROME_ONLY, ERROR_PAGE, READER_ENVELOPE
from processors.fetch_pipeline import (
    REASON_NOT_FOUND,
    REASON_REJECTED,
    REASON_TOO_SHORT,
    domain_session,
    fetch_article,
    reset_domain_sessions,
)
from processors.record_writer import (
    MIN_QUALITY_CHARS,
    build_news_record,
    build_topic_record,
    quality_of,
)


ARTICLE_HTML = "<html><body><article><p>" + (
    "Sepak bola Indonesia berkembang pesat di kancah regional dan internasional. " * 6
    ) + "</p></article></body></html>"


class FakeCurlBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.proxies = []
        self.calls = []

    async def fetch_async(self, session, url, proxy=None, cookies=None, user_agent=None):
        self.proxies.append(proxy)
        self.calls.append({"proxy": proxy, "cookies": cookies, "user_agent": user_agent})
        return self.responses.pop(0)


class FakeChannel:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def extract(self, url, config=None):
        self.calls += 1
        return self.result


class TestFetchArticleLadder(unittest.TestCase):
    """Verifies the direct, proxy, and channel fallback ordering."""

    def _run(self, curl, channel, min_length=150, fallback_min_length=None,
             min_quality_chars=MIN_QUALITY_CHARS, wp_api_base=None):
        return asyncio.run(
            fetch_article(
                session=object(),
                curl_backend=curl,
                channel=channel,
                url="https://news.example.com/a",
                min_content_length=min_length,
                fallback_min_content_length=fallback_min_length,
                min_quality_chars=min_quality_chars,
                wp_api_base=wp_api_base,
            )
        )

    def test_404_short_circuits_without_proxy_or_channel(self):
        curl = FakeCurlBackend([(404, None)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        res = self._run(curl, channel)
        self.assertEqual(res.reason, REASON_NOT_FOUND)
        self.assertIsNone(res.content)
        self.assertEqual(curl.proxies, [None])
        self.assertEqual(channel.calls, 0)

    def test_direct_success_skips_channel(self):
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertEqual(res.backend, 'curl_impersonate')
        self.assertGreaterEqual(len(res.content), 150)
        self.assertEqual(channel.calls, 0)

    def test_blocked_status_retries_through_proxy(self):
        curl = FakeCurlBackend([(403, None), (200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        with patch('processors.fetch_pipeline.default_http_client') as client:
            client.get_active_proxy.return_value = 'http://proxy:8080'
            res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertEqual(curl.proxies, [None, 'http://proxy:8080'])

    def test_short_direct_content_falls_back_to_channel(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({
            'status': 'success',
            'content': 'x' * 200,
            'backend': 'direct_http',
            'title': 'Judul',
            'published_date': '2026-10-20',
        })
        res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertEqual(res.backend, 'direct_http')
        self.assertEqual(res.title, 'Judul')
        self.assertEqual(channel.calls, 1)

    def test_channel_failure_reports_threshold_reason(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'failed', 'content': ''})
        res = self._run(curl, channel)
        self.assertEqual(res.reason, REASON_TOO_SHORT)
        self.assertIsNone(res.content)

    def test_fallback_threshold_is_separate_from_direct(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 20})
        res = self._run(curl, channel, min_length=150, fallback_min_length=1)
        self.assertIsNone(res.reason)
        self.assertEqual(len(res.content), 20)

    def test_dateline_is_stripped_before_the_length_gate(self):
        # 32 chars before normalization, 22 after: the gate must see the 22.
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'failed', 'content': ''})
        with patch(
            'processors.fetch_pipeline.extract_clean_article_text',
            return_value='Jakarta - Laga berjalan sengit.',
        ):
            res = self._run(curl, channel, min_length=25)
        self.assertEqual(res.reason, REASON_TOO_SHORT)
        self.assertIsNone(res.content)

    def test_direct_content_is_returned_without_the_dateline(self):
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'failed', 'content': ''})
        with patch(
            'processors.fetch_pipeline.extract_clean_article_text',
            return_value='Jakarta - ' + 'Laga berjalan sengit. ' * 10,
        ):
            res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertTrue(res.content.startswith('Laga berjalan'))
        self.assertNotIn('Jakarta', res.content)

    def test_channel_fallback_content_is_normalized_too(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({
            'status': 'success',
            'content': 'KOMPAS.com - ' + 'Laga berjalan sengit. ' * 10,
            'backend': 'direct_http',
        })
        res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertTrue(res.content.startswith('Laga berjalan'))
        self.assertNotIn('KOMPAS.com', res.content)

    def test_success_carries_body_length_and_shape_label(self):
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        res = self._run(curl, channel)
        self.assertEqual(res.content_chars, len(res.content))
        self.assertEqual(res.quality, "ok")
        self.assertEqual(res.label, ARTICLE)

    def test_short_clean_body_is_tagged_ok_not_low(self):
        # "low" now means a photo caption or an ad-interleaved body. A short
        # clean paragraph is a fragment, and length no longer decides its label.
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 20})
        res = self._run(curl, channel, min_length=150, fallback_min_length=1)
        self.assertEqual(res.quality, "ok")
        self.assertEqual(res.content_chars, 20)

    def test_min_quality_chars_no_longer_changes_the_label(self):
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        res = self._run(curl, channel, min_quality_chars=100000)
        self.assertEqual(res.quality, "ok")

    def test_reader_envelope_is_not_returned_as_content(self):
        envelope = (
            "Title: Hasil PSS vs Borneo\nURL Source: https://bola.example.com/x\n\n"
            "Markdown Content:\n\n" + "Isi berita yang cukup panjang. " * 12
        )
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': envelope})
        res = self._run(curl, channel, min_length=150, fallback_min_length=1)
        self.assertIsNone(res.content)
        self.assertEqual(res.reason, REASON_REJECTED)
        self.assertEqual(res.label, READER_ENVELOPE)

    def test_chrome_only_is_rejected_at_the_channel_rung(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': 'Detail Berita Link Copied! ' * 12})
        res = self._run(curl, channel, min_length=150, fallback_min_length=1)
        self.assertIsNone(res.content)
        self.assertEqual(res.reason, REASON_REJECTED)
        self.assertEqual(res.label, CHROME_ONLY)

    def test_rejected_body_reports_a_shape_reason(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': 'The request could not be satisfied. ' * 5})
        res = self._run(curl, channel, min_length=150, fallback_min_length=1)
        self.assertIsNone(res.content)
        self.assertEqual(res.reason, REASON_REJECTED)
        self.assertEqual(res.label, ERROR_PAGE)

    def test_wp_rest_body_is_classified_too(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'failed', 'content': ''})
        post = {'content': 'URL Source: https://a\nMarkdown Content:\n' + 'isi ' * 100, 'title': 'T'}
        with patch('processors.fetch_pipeline.fetch_post_body', new=AsyncMock(return_value=post)):
            res = self._run(curl, channel, wp_api_base='https://a.example.com/wp-json')
        self.assertIsNone(res.content)
        self.assertEqual(res.label, READER_ENVELOPE)
        self.assertEqual(channel.calls, 1)


class TestRecordShapes(unittest.TestCase):
    """Locks the exact key set of both harvest record schemas."""

    def test_news_record_keys(self):
        rec = build_news_record("https://a.example.com/x", "T", "2026-01-01", "", "body", "curl_impersonate")
        self.assertEqual(
            set(rec),
            {
                "url", "title", "published_date", "archive_date", "content",
                "source", "backend", "quality", "content_chars", "harvested_at",
            },
        )
        self.assertEqual(rec["source"], "a.example.com")

    def test_topic_record_keys(self):
        rec = build_topic_record("https://a.example.com/x", "T", "body", "sports", "detik", "2026-01-01")
        self.assertEqual(
            set(rec),
            {
                "url", "title", "content", "topic", "portal", "publish_date",
                "quality", "content_chars", "harvested_at",
            },
        )
        self.assertEqual(rec["topic"], "sports")

    def test_news_record_without_quality_uses_the_deprecated_length_rule(self):
        # build_news_record is unchanged on purpose; this locks the fallback for
        # callers that pass no quality, not the behaviour of the fetch pipeline.
        rec = build_news_record("https://a.example.com/x", "T", "", "", "x" * 405, "direct_http")
        self.assertEqual(rec["quality"], "low")
        self.assertEqual(rec["content_chars"], 405)

    def test_news_record_tags_healthy_body_ok(self):
        rec = build_news_record("https://a.example.com/x", "T", "", "", "x" * 4000, "curl_impersonate")
        self.assertEqual(rec["quality"], "ok")
        self.assertEqual(rec["content_chars"], 4000)

    def test_topic_record_uses_explicit_quality_over_length(self):
        rec = build_topic_record(
            "https://a.example.com/x", "T", "x" * 4000, "sports", "detik", "",
            quality="low", content_chars=4000,
        )
        self.assertEqual(rec["quality"], "low")

    def test_quality_of_is_retained_for_legacy_callers(self):
        self.assertEqual(quality_of(MIN_QUALITY_CHARS), "ok")
        self.assertEqual(quality_of(MIN_QUALITY_CHARS - 1), "low")


CHALLENGE_HTML = "<html><head><title>Just a moment...</title></head><body>cf-chl-bypass</body></html>"


class TestDomainSessionClearance(unittest.TestCase):
    """A stored browser session is replayed only when the WAF blocks the request."""

    def setUp(self):
        reset_domain_sessions()

    def tearDown(self):
        reset_domain_sessions()

    def _run(self, curl, channel):
        return asyncio.run(
            fetch_article(
                session=object(),
                curl_backend=curl,
                channel=channel,
                url="https://news.example.com/a",
                min_content_length=150,
            )
        )

    def _stored_session(self, cookies=None, user_agent="Mozilla/5.0 captured"):
        return (
            patch("processors.fetch_pipeline.session_store.load", return_value=cookies or {"cf_clearance": "tok"}),
            patch("processors.fetch_pipeline.session_store.load_user_agent", return_value=user_agent),
        )

    def test_challenge_without_session_falls_back_to_channel(self):
        curl = FakeCurlBackend([(202, CHALLENGE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        with patch("processors.fetch_pipeline.domain_session", return_value=None):
            res = self._run(curl, channel)
        self.assertEqual(res.backend, 'direct_http')
        self.assertEqual(channel.calls, 1)
        self.assertEqual(len(curl.calls), 1)

    def test_challenge_is_retried_with_the_stored_session(self):
        curl = FakeCurlBackend([(202, CHALLENGE_HTML), (200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        load_patch, ua_patch = self._stored_session()
        with load_patch, ua_patch:
            res = self._run(curl, channel)
        self.assertIsNone(res.reason)
        self.assertEqual(res.backend, 'curl_session')
        self.assertEqual(channel.calls, 0)
        self.assertEqual(curl.calls[-1]["cookies"], {"cf_clearance": "tok"})
        self.assertEqual(curl.calls[-1]["user_agent"], "Mozilla/5.0 captured")

    def test_session_is_tried_after_the_proxy_rung(self):
        curl = FakeCurlBackend([(403, ""), (403, ""), (200, ARTICLE_HTML)])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        load_patch, ua_patch = self._stored_session()
        with patch("processors.fetch_pipeline.default_http_client") as client, load_patch, ua_patch:
            client.get_active_proxy.return_value = "http://proxy:8080"
            res = self._run(curl, channel)
        self.assertEqual(res.backend, 'curl_session')
        self.assertEqual([call["proxy"] for call in curl.calls], [None, "http://proxy:8080", None])

    def test_short_page_does_not_trigger_a_session_retry(self):
        curl = FakeCurlBackend([(200, '<html><body><p>pendek</p></body></html>')])
        channel = FakeChannel({'status': 'success', 'content': 'x' * 200})
        load_patch, ua_patch = self._stored_session()
        with load_patch as load, ua_patch:
            res = self._run(curl, channel)
        self.assertEqual(res.backend, 'direct_http')
        load.assert_not_called()

    def test_session_lookup_is_cached_per_domain(self):
        with patch("processors.fetch_pipeline.session_store.load", return_value={"a": "b"}) as load, \
             patch("processors.fetch_pipeline.session_store.load_user_agent", return_value="ua"):
            self.assertEqual(domain_session("news.example.com"), ({"a": "b"}, "ua"))
            self.assertEqual(domain_session("news.example.com"), ({"a": "b"}, "ua"))
        self.assertEqual(load.call_count, 1)

    def test_unknown_domain_has_no_session(self):
        with patch("processors.fetch_pipeline.session_store.load", return_value=None):
            self.assertIsNone(domain_session("empty.example.com"))


if __name__ == "__main__":
    unittest.main()
