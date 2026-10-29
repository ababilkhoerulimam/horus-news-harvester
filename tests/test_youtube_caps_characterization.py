"""Pins the current YouTube depth caps before unlimited harvesting replaces them."""

import unittest
from unittest.mock import patch

from channels.social_youtube import YouTubeChannel


def _comment_page(comment_id, next_token):
    return {
        "frameworkUpdates": {
            "entityBatchUpdate": {
                "mutations": [
                    {
                        "payload": {
                            "commentEntityPayload": {
                                "properties": {
                                    "commentId": comment_id,
                                    "content": {"content": "komentar"},
                                },
                                "author": {"displayName": "pengguna"},
                                "toolbar": {"likeCountNotliked": "1"},
                            }
                        }
                    }
                ]
            }
        },
        "onResponseReceivedEndpoints": [
            {
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {
                            "continuationItemRenderer": {
                                "button": {
                                    "buttonRenderer": {
                                        "command": {
                                            "continuationCommand": {"token": next_token},
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        ],
    }


def _reply_page(reply_id, next_token):
    return {
        "frameworkUpdates": {
            "entityBatchUpdate": {
                "mutations": [
                    {
                        "payload": {
                            "commentEntityPayload": {
                                "properties": {
                                    "commentId": reply_id,
                                    "content": {"content": "balasan"},
                                },
                                "author": {"displayName": "pengguna"},
                                "toolbar": {"likeCountNotliked": "2"},
                            }
                        }
                    }
                ]
            }
        },
        "onResponseReceivedEndpoints": [
            {
                "appendContinuationItemsAction": {
                    "continuationItems": [
                        {
                            "continuationItemRenderer": {
                                "button": {
                                    "buttonRenderer": {
                                        "command": {
                                            "continuationCommand": {"token": next_token},
                                        }
                                    }
                                }
                            }
                        }
                    ]
                }
            }
        ],
    }


def _watch_page(comment_token):
    return {
        "contents": {
            "twoColumnWatchNextResults": {
                "results": {
                    "results": {
                        "contents": [
                            {"videoPrimaryInfoRenderer": {"title": {"runs": [{"text": "Judul"}]}}},
                            {
                                "itemSectionRenderer": {
                                    "contents": [
                                        {
                                            "continuationItemRenderer": {
                                                "continuationEndpoint": {
                                                    "continuationCommand": {"token": comment_token},
                                                }
                                            }
                                        }
                                    ]
                                }
                            },
                        ]
                    }
                }
            }
        }
    }


class CommentBudgetTests(unittest.TestCase):
    def test_capped_comment_loop_stops_at_record_budget(self):
        channel = YouTubeChannel()
        calls = []

        def fake_post(endpoint, payload, timeout=12, use_jitter=False):
            calls.append(payload.get("continuation"))
            return _comment_page(f"c{len(calls)}", f"tok{len(calls) + 1}")

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            harvest = channel._fetch_innertube_comments("tok1", max_comments=100, fetch_replies=False)

        self.assertEqual(len(calls), 100)
        self.assertEqual(len(harvest.comments), 100)
        self.assertEqual(harvest.stop_reason, "max_comments")

    def test_capped_reply_loop_uses_page_budget(self):
        channel = YouTubeChannel()
        calls = []

        def fake_post(endpoint, payload, timeout=10, use_jitter=False):
            calls.append(payload.get("continuation"))
            return _reply_page(f"r{len(calls)}", f"next{len(calls) + 1}")

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            replies = channel._fetch_innertube_replies("start", max_replies=200)

        self.assertEqual(len(calls), 20)
        self.assertEqual(len(replies), 20)


class PageErrorTests(unittest.TestCase):
    def test_page_error_still_returns_success(self):
        channel = YouTubeChannel()

        def fake_post(endpoint, payload, timeout=12, use_jitter=False):
            if "continuation" not in payload:
                return _watch_page("comment_token")
            raise RuntimeError("halaman gagal")

        with patch("channels.social_youtube.post_innertube", side_effect=fake_post):
            result = channel.extract(
                "https://www.youtube.com/watch?v=abcdefghijk",
                config={"social_youtube_backend": "innertube_api"},
            )

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["comments"], [])


if __name__ == "__main__":
    unittest.main()