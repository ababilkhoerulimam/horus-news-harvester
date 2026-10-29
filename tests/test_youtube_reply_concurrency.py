"""
Reply thread concurrency: a worker pool must return exactly what the serial loop
returns, in the same order, and must survive one failing thread.
"""

import threading
import unittest
from unittest.mock import patch

from channels.social_youtube import YouTubeChannel


def _comment_mutations(comment_ids):
    return [
        {
            "payload": {
                "commentEntityPayload": {
                    "properties": {"commentId": cid, "content": {"content": "komentar"}},
                    "author": {"displayName": "pengguna"},
                    "toolbar": {"likeCountNotliked": "3"},
                }
            }
        }
        for cid in comment_ids
    ]


def _thread_item(cid, reply_token):
    return {
        "commentThreadRenderer": {
            "commentViewModel": {"commentViewModel": {"commentId": cid}},
            "replies": {
                "commentRepliesRenderer": {
                    "contents": [
                        {
                            "continuationItemRenderer": {
                                "button": {
                                    "buttonRenderer": {
                                        "command": {"continuationCommand": {"token": reply_token}}
                                    }
                                }
                            }
                        }
                    ]
                }
            },
        }
    }


def _root_page(count):
    """One comment page where every root advertises a reply thread."""
    ids = [f"c{index}" for index in range(count)]
    return {
        "frameworkUpdates": {"entityBatchUpdate": {"mutations": _comment_mutations(ids)}},
        "onResponseReceivedEndpoints": [
            {
                "appendContinuationItemsAction": {
                    "continuationItems": [_thread_item(cid, f"reply:{cid}") for cid in ids]
                }
            }
        ],
    }


def _reply_page(cid):
    ids = [f"{cid}_r0", f"{cid}_r1"]
    return {
        "frameworkUpdates": {"entityBatchUpdate": {"mutations": _comment_mutations(ids)}},
        "onResponseReceivedEndpoints": [],
    }


class ReplyConcurrencyTests(unittest.TestCase):
    ROOTS = 20

    def setUp(self):
        self.channel = YouTubeChannel()
        self.threads = set()

    def _fake_post(self, endpoint, payload, timeout=12, use_jitter=False):
        self.threads.add(threading.get_ident())
        token = payload.get("continuation") or ""
        if token == "root_page":
            return _root_page(self.ROOTS)
        if token.startswith("reply:"):
            return _reply_page(token.split(":", 1)[1])
        return None

    def _harvest(self, workers):
        with patch("channels.social_youtube.post_innertube", side_effect=self._fake_post):
            return self.channel._fetch_innertube_comments("root_page", reply_workers=workers)

    def test_pool_matches_serial_output(self):
        serial = self._harvest(1)
        self.threads.clear()
        pooled = self._harvest(4)

        self.assertEqual(
            [c["id"] for c in serial.comments], [c["id"] for c in pooled.comments]
        )
        for serial_comment, pooled_comment in zip(serial.comments, pooled.comments):
            self.assertEqual(
                [r["id"] for r in serial_comment["replies"]],
                [r["id"] for r in pooled_comment["replies"]],
            )
        self.assertEqual(serial.replies_fetched, pooled.replies_fetched)
        self.assertEqual(serial.stop_reason, pooled.stop_reason)
        self.assertEqual(pooled.stop_reason, "exhausted")
        self.assertEqual(pooled.replies_fetched, self.ROOTS * 2)

    def test_serial_run_uses_one_thread(self):
        self._harvest(1)
        self.assertEqual(len(self.threads), 1)

    def test_pool_uses_more_than_one_thread(self):
        self._harvest(4)
        self.assertGreater(len(self.threads), 1)

    def test_one_failing_thread_keeps_the_rest(self):
        original = YouTubeChannel._fetch_innertube_replies

        def flaky(channel_self, reply_token, **kwargs):
            if reply_token == "reply:c7":
                raise RuntimeError("boom")
            return original(channel_self, reply_token, **kwargs)

        with patch.object(YouTubeChannel, "_fetch_innertube_replies", flaky):
            harvest = self._harvest(4)

        self.assertEqual(len(harvest.comments), self.ROOTS)
        self.assertEqual(harvest.comments[7]["replies"], [])
        self.assertEqual(harvest.comments[8]["replies"][0]["id"], "c8_r0")
        self.assertEqual(harvest.replies_fetched, (self.ROOTS - 1) * 2)

    def test_workers_one_skips_the_pool_entirely(self):
        with patch("channels.social_youtube.ThreadPoolExecutor") as mock_pool:
            self._harvest(1)
        mock_pool.assert_not_called()

    def test_workers_above_one_builds_one_pool(self):
        with patch("channels.social_youtube.ThreadPoolExecutor") as mock_pool:
            self._harvest(4)
        mock_pool.assert_called_once_with(max_workers=4)


if __name__ == "__main__":
    unittest.main()
