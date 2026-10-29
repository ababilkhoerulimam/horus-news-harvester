"""Unlimited YouTube harvesting: termination guards and config semantics."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import pydantic

from channels.social_youtube import YouTubeChannel
from processors.topic_config import SocialConfig, TopicConfig, YouTubeSocialConfig
from scripts import social_harvester


def _page(comment_ids, next_token, with_reply_token=False):
    mutations = [
        {
            "payload": {
                "commentEntityPayload": {
                    "properties": {"commentId": cid, "content": {"content": "komentar"}},
                    "author": {"displayName": "pengguna"},
                    "toolbar": {"likeCountNotliked": "1"},
                }
            }
        }
        for cid in comment_ids
    ]
    items = []
    if next_token:
        items.append({
            "continuationItemRenderer": {
                "button": {"buttonRenderer": {"command": {"continuationCommand": {"token": next_token}}}},
            }
        })
    return {
        "frameworkUpdates": {"entityBatchUpdate": {"mutations": mutations}},
        "onResponseReceivedEndpoints": [{"appendContinuationItemsAction": {"continuationItems": items}}],
    }


class UnlimitedCommentLoopTests(unittest.TestCase):
    def setUp(self):
        self.channel = YouTubeChannel()

    def test_stops_on_empty_token(self):
        with patch("channels.social_youtube.post_innertube", return_value=_page(["c1"], None)):
            harvest = self.channel._fetch_innertube_comments("tok1", fetch_replies=False)

        self.assertEqual(len(harvest.comments), 1)
        self.assertEqual(harvest.stop_reason, "exhausted")
        self.assertEqual(harvest.pages, 1)

    def test_has_no_page_ceiling(self):
        calls = []

        def fake_post(endpoint, payload, timeout=12, use_jitter=False):
            calls.append(payload.get("continuation"))
            if len(calls) > 300:
                return _page([f"c{len(calls)}"], None)
            return _page([f"c{len(calls)}"], f"tok{len(calls) + 1}")

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            harvest = self.channel._fetch_innertube_comments("tok1", fetch_replies=False)

        self.assertEqual(len(harvest.comments), 301)
        self.assertEqual(harvest.pages, 301)
        self.assertEqual(harvest.stop_reason, "exhausted")

    def test_repeated_token_stops_loop(self):
        with patch("channels.social_youtube.post_innertube", return_value=_page(["c1"], "same_token")):
            harvest = self.channel._fetch_innertube_comments("same_token", fetch_replies=False)

        self.assertEqual(harvest.stop_reason, "repeated_token")
        self.assertEqual(harvest.pages, 1)

    def test_no_progress_stops_loop(self):
        calls = []

        def fake_post(endpoint, payload, timeout=12, use_jitter=False):
            calls.append(payload.get("continuation"))
            return _page([], f"tok{len(calls) + 1}")

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            harvest = self.channel._fetch_innertube_comments("tok1", fetch_replies=False)

        self.assertEqual(harvest.stop_reason, "no_progress")
        self.assertEqual(len(calls), 2)

    def test_deadline_truncates_before_first_request(self):
        calls = []

        def fake_post(endpoint, payload, timeout=12, use_jitter=False):
            calls.append(payload)
            return _page(["c1"], None)

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            harvest = self.channel._fetch_innertube_comments(
                "tok1", fetch_replies=False, deadline=time.monotonic() - 1
            )

        self.assertEqual(harvest.stop_reason, "runtime_budget")
        self.assertEqual(calls, [])

    def test_unlimited_budget_keeps_every_comment_in_page(self):
        with patch("channels.social_youtube.post_innertube", return_value=_page(["c1", "c2", "c3"], None)):
            harvest = self.channel._fetch_innertube_comments("tok1", max_comments=None, fetch_replies=False)

        self.assertEqual(len(harvest.comments), 3)


class ConfigCapTests(unittest.TestCase):
    def test_zero_cap_is_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            YouTubeSocialConfig(max_comments_per_video=0)

    def test_negative_cap_is_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            YouTubeSocialConfig(max_replies_per_comment=-1)

    def test_omitted_and_null_caps_mean_unlimited(self):
        self.assertIsNone(YouTubeSocialConfig().max_comments_per_video)
        self.assertIsNone(YouTubeSocialConfig(max_comments_per_video=None).max_comments_per_video)


class TruncatedVideoTests(unittest.TestCase):
    def test_truncated_video_is_requeued(self):
        cfg = TopicConfig.model_validate({
            "topic": {"id": "sports", "name": "Sports"},
            "keywords": ["timnas"],
            "social": {
                "enabled": True,
                "youtube": {"queries": ["timnas"], "max_videos_per_run": 1},
            },
        })

        class TruncatingChannel:
            def search(self, query, sport="football", limit=10, config=None):
                return [{"video_id": "v1", "title": "t", "published_date": "2026-10-20"}]

            def extract(self, url, config=None):
                return {
                    "url": url,
                    "video_id": "v1",
                    "title": "t",
                    "content": "body",
                    "comment_count": 1,
                    "comments": [{"id": "c1", "text": "x"}],
                    "backend": "innertube_api",
                    "status": "success",
                    "stop_reason": "runtime_budget",
                    "truncated": True,
                    "pages": 2,
                }

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(social_harvester, "YOUTUBE_DIR", root / "youtube"), \
                 patch.object(social_harvester, "FRONTIER_TEMPLATE", str(root / "{topic}_{platform}.db")), \
                 patch.object(social_harvester, "YouTubeChannel", return_value=TruncatingChannel()), \
                 patch.object(social_harvester, "refresh_client_version", return_value="2.0"):
                result = social_harvester.harvest_youtube(cfg)

        self.assertEqual(result["truncated"], 1)
        self.assertEqual(result["coverage"][0]["status"], "truncated")


if __name__ == "__main__":
    unittest.main()