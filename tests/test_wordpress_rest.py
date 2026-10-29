"""
WordPress REST article body: slug handling, payload parsing, and the pipeline
preference order.
"""

import asyncio
import json
import unittest

from backends.wordpress_rest import (
    endpoint_for,
    parse_post_payload,
    slug_from_url,
)
from processors.fetch_pipeline import fetch_article


ARTICLE_HTML = "<html><body><article><p>" + (
    "Sepak bola Indonesia berkembang pesat di kancah regional dan internasional. " * 6
) + "</p></article></body></html>"


def _wp_payload(chars=400, title="Judul WP"):
    body = "<p>" + ("Isi artikel dari WordPress REST. " * (chars // 30 + 1)) + "</p>"
    return json.dumps(
        [{"content": {"rendered": body}, "title": {"rendered": title}, "link": "https://x/y"}]
    ).encode("utf-8")


class FakeCurlBackend:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    async def fetch_async(self, session, url, proxy=None):
        self.urls.append(url)
        return self.responses.pop(0)


class FakeChannel:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def extract(self, url, config=None):
        self.calls += 1
        return self.result


class SlugTests(unittest.TestCase):
    def test_plain_slug(self):
        self.assertEqual(slug_from_url("https://fajar.co.id/2026/10/22/judul-artikel/"), "judul-artikel")

    def test_query_string_is_ignored(self):
        self.assertEqual(
            slug_from_url("https://www.balipost.com/news/2026/10/20/590529/Jadwal-Liga.html?page=all"),
            "Jadwal-Liga",
        )

    def test_html_suffix_is_stripped(self):
        self.assertEqual(slug_from_url("https://x.com/a/b/judul.html"), "judul")

    def test_root_url_has_no_slug(self):
        self.assertIsNone(slug_from_url("https://x.com/"))

    def test_empty_url(self):
        self.assertIsNone(slug_from_url(""))


class EndpointTests(unittest.TestCase):
    def test_base_without_posts_segment(self):
        self.assertEqual(
            endpoint_for("https://x.com/wp-json/wp/v2", "https://x.com/2026/10/18/judul"),
            "https://x.com/wp-json/wp/v2/posts?slug=judul&_fields=content.rendered,title.rendered,link",
        )

    def test_base_with_posts_segment_is_not_doubled(self):
        endpoint = endpoint_for(
            "https://x.com/wp-json/wp/v2/posts", "https://x.com/2026/10/18/judul"
        )
        self.assertNotIn("/posts/posts", endpoint)
        self.assertIn("slug=judul", endpoint)

    def test_missing_base_returns_none(self):
        self.assertIsNone(endpoint_for("", "https://x.com/a/b"))


class PayloadTests(unittest.TestCase):
    def test_valid_payload(self):
        parsed = parse_post_payload(_wp_payload())
        self.assertIsNotNone(parsed)
        self.assertGreater(len(parsed["content"]), 200)
        self.assertEqual(parsed["title"], "Judul WP")

    def test_empty_list(self):
        self.assertIsNone(parse_post_payload(b"[]"))

    def test_not_json(self):
        self.assertIsNone(parse_post_payload(b"<html>nope</html>"))

    def test_missing_content(self):
        self.assertIsNone(parse_post_payload(json.dumps([{"title": {"rendered": "x"}}]).encode()))

    def test_empty_body(self):
        self.assertIsNone(parse_post_payload(b""))


class PipelinePreferenceTests(unittest.TestCase):
    def _run(self, curl, channel, wp_api_base, min_length=150):
        return asyncio.run(
            fetch_article(
                session=object(),
                curl_backend=curl,
                channel=channel,
                url="https://fajar.co.id/2026/10/22/judul-artikel/",
                min_content_length=min_length,
                fallback_min_content_length=1,
                wp_api_base=wp_api_base,
            )
        )

    def test_wp_rest_wins_before_html(self):
        curl = FakeCurlBackend([(200, _wp_payload())])
        channel = FakeChannel({"status": "failed"})
        res = self._run(curl, channel, "https://fajar.co.id/wp-json/wp/v2/posts")
        self.assertEqual(res.backend, "wordpress_rest")
        self.assertEqual(res.title, "Judul WP")
        self.assertEqual(channel.calls, 0)
        self.assertIn("/wp-json/wp/v2/posts?slug=judul-artikel", curl.urls[0])

    def test_short_wp_payload_falls_through_to_html(self):
        curl = FakeCurlBackend([(200, _wp_payload(chars=30)), (200, ARTICLE_HTML)])
        channel = FakeChannel({"status": "failed"})
        res = self._run(curl, channel, "https://fajar.co.id/wp-json/wp/v2/posts")
        self.assertEqual(res.backend, "curl_impersonate")
        self.assertEqual(len(curl.urls), 2)

    def test_wp_rest_error_falls_through(self):
        curl = FakeCurlBackend([(500, b""), (200, ARTICLE_HTML)])
        channel = FakeChannel({"status": "failed"})
        res = self._run(curl, channel, "https://fajar.co.id/wp-json/wp/v2/posts")
        self.assertEqual(res.backend, "curl_impersonate")

    def test_no_wp_base_skips_the_json_path(self):
        curl = FakeCurlBackend([(200, ARTICLE_HTML)])
        channel = FakeChannel({"status": "failed"})
        res = self._run(curl, channel, None)
        self.assertEqual(res.backend, "curl_impersonate")
        self.assertEqual(len(curl.urls), 1)
        self.assertNotIn("wp-json", curl.urls[0])


if __name__ == "__main__":
    unittest.main()
