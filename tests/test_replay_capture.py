"""Tests for HTML capture and offline replay of the extraction ladder."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from processors.fetch_pipeline import fetch_article
from scripts.probe_extraction import HtmlStore, ReplayMiss

URL = "https://news.example.com/a"
ARTICLE_HTML = "<html><body><article><p>" + (
    "Sepak bola Indonesia berkembang pesat di kancah regional dan internasional. " * 6
) + "</p></article></body></html>"


class FakeStore:
    """In-memory stand-in for HtmlStore; records every read and write."""

    def __init__(self, captures=None, replay=False):
        self.captures = dict(captures or {})
        self.reads = []
        self.writes = []
        self.replay = replay

    def read(self, url):
        if not self.replay:
            return None
        self.reads.append(url)
        if url in self.captures:
            return self.captures[url]
        raise LookupError(url)

    def write(self, url, status, html, backend=""):
        if self.replay:
            return
        self.writes.append((url, status, backend))
        self.captures[url] = (status, html or "")


class RaisingCurl:
    """A backend that fails the test if replay ever reaches the network."""

    async def fetch_async(self, *args, **kwargs):
        raise AssertionError("network used during replay")


class FakeCurl:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def fetch_async(self, session, url, proxy=None, cookies=None, user_agent=None):
        self.calls.append({"url": url, "proxy": proxy})
        return self.responses.pop(0)


class RecordingChannel:
    def __init__(self, result):
        self.result = result
        self.configs = []

    def extract(self, url, config=None):
        self.configs.append(dict(config or {}))
        return self.result


def _run(curl, channel, store=None, min_length=150):
    return asyncio.run(
        fetch_article(
            session=object(),
            curl_backend=curl,
            channel=channel,
            url=URL,
            min_content_length=min_length,
            fallback_min_content_length=1,
            html_source=store,
        )
    )


class ReplayTests(unittest.TestCase):
    def test_replay_without_network(self):
        store = FakeStore({URL: (200, ARTICLE_HTML)}, replay=True)
        channel = RecordingChannel({"status": "failed", "content": ""})

        result = _run(RaisingCurl(), channel, store)

        self.assertIsNone(result.reason)
        self.assertEqual(result.backend, "curl_impersonate")
        self.assertEqual(store.reads, [URL])
        self.assertEqual(store.writes, [])

    def test_replay_skips_the_proxy_rung(self):
        store = FakeStore({URL: (200, ARTICLE_HTML)}, replay=True)
        channel = RecordingChannel({"status": "failed", "content": ""})

        with patch("processors.fetch_pipeline.default_http_client") as client:
            client.get_active_proxy.return_value = "http://proxy:8080"
            result = _run(RaisingCurl(), channel, store)

        self.assertIsNone(result.reason)
        self.assertEqual(channel.configs, [])

    def test_replay_returns_a_captured_404(self):
        store = FakeStore({URL: (404, "")}, replay=True)
        channel = RecordingChannel({"status": "success", "content": "x" * 200})

        result = _run(RaisingCurl(), channel, store)

        self.assertIsNone(result.content)
        self.assertEqual(result.reason, "HTTP 404 Not Found")
        self.assertEqual(channel.configs, [])


class CaptureTests(unittest.TestCase):
    def test_capture_writes_the_first_rung(self):
        store = FakeStore()
        curl = FakeCurl([(200, ARTICLE_HTML)])

        result = _run(curl, RecordingChannel({"status": "failed", "content": ""}), store)

        self.assertIsNone(result.reason)
        self.assertEqual(store.writes, [(URL, 200, "curl_impersonate")])

    def test_capture_writes_the_proxy_rung(self):
        store = FakeStore()
        curl = FakeCurl([(403, ""), (200, ARTICLE_HTML)])

        with patch("processors.fetch_pipeline.default_http_client") as client:
            client.get_active_proxy.return_value = "http://proxy:8080"
            result = _run(curl, RecordingChannel({"status": "failed", "content": ""}), store)

        self.assertIsNone(result.reason)
        self.assertEqual(len(store.writes), 2)
        self.assertEqual(curl.calls[1]["proxy"], "http://proxy:8080")

    def test_capture_is_off_by_default(self):
        store = FakeStore()
        curl = FakeCurl([(200, ARTICLE_HTML)])

        result = _run(curl, RecordingChannel({"status": "failed", "content": ""}))

        self.assertIsNone(result.reason)
        self.assertEqual(store.reads, [])
        self.assertEqual(store.writes, [])

    def test_channel_receives_the_html_sink(self):
        store = FakeStore()
        curl = FakeCurl([(200, "<html><body><p>pendek</p></body></html>")])
        channel = RecordingChannel({"status": "failed", "content": ""})

        _run(curl, channel, store)

        self.assertIs(channel.configs[0]["html_sink"], store)

    def test_channel_gets_no_sink_without_a_store(self):
        curl = FakeCurl([(200, "<html><body><p>pendek</p></body></html>")])
        channel = RecordingChannel({"status": "failed", "content": ""})

        _run(curl, channel)

        self.assertNotIn("html_sink", channel.configs[0])


class HtmlStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sidecar_roundtrip(self):
        HtmlStore(self.root).write(URL, 200, ARTICLE_HTML, "curl_impersonate")

        self.assertEqual(HtmlStore(self.root, replay=True).read(URL), (200, ARTICLE_HTML))
        meta = json.loads(next(self.root.glob("*/*.json")).read_text(encoding="utf-8"))
        self.assertEqual(
            set(meta),
            {"url", "status", "backend", "fetched_at", "bytes", "sha256"},
        )

    def test_orphan_page_is_a_replay_miss(self):
        # A page without its sidecar cannot be replayed honestly: the status it
        # was fetched with is gone, and inventing one is what the sidecar exists
        # to prevent.
        HtmlStore(self.root).write(URL, 200, ARTICLE_HTML, "curl_impersonate")
        next(self.root.glob("*/*.json")).unlink()

        with self.assertRaises(ReplayMiss):
            HtmlStore(self.root, replay=True).read(URL)

    def test_capturing_store_never_serves_its_own_write(self):
        store = HtmlStore(self.root)
        store.write(URL, 403, "")

        self.assertIsNone(store.read(URL))

    def test_replay_store_never_writes(self):
        store = HtmlStore(self.root, replay=True)
        store.write(URL, 200, ARTICLE_HTML)

        self.assertEqual(list(self.root.glob("*/*.json")), [])

    def test_replay_store_raises_on_a_miss(self):
        store = HtmlStore(self.root, replay=True)
        with self.assertRaises(ReplayMiss):
            store.read(URL)

    def test_sha1_is_stable_and_different(self):
        store = HtmlStore(self.root)
        first = store._paths(URL)
        second = store._paths(URL)
        other = store._paths("https://news.example.com/b")

        self.assertEqual(first, second)
        self.assertNotEqual(first[0], other[0])

    def test_domain_directory_is_used(self):
        store = HtmlStore(self.root)
        page, _ = store._paths("https://www.bola.example.com/x")

        self.assertEqual(page.parent.name, "bola.example.com")

    def test_report_counts_pages_per_domain(self):
        store = HtmlStore(self.root)
        store.write("https://a.example.com/1", 200, "x", "curl_impersonate")
        store.write("https://a.example.com/2", 404, "", "curl_impersonate")
        store.write("https://b.example.com/1", 200, "y", "direct_http")

        report = store.report()

        self.assertEqual(report["a.example.com"]["pages"], 2)
        self.assertEqual(report["a.example.com"]["statuses"], {"200": 1, "404": 1})
        self.assertEqual(report["b.example.com"]["pages"], 1)


if __name__ == "__main__":
    unittest.main()
