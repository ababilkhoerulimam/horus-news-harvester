"""Twitter pagination, rate-limit and tweet-by-id behaviour."""

import json
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from backends.twitter_graphql import TwitterGraphQLBackend
from channels.social_twitter import TwitterChannel


def _tweet_payload(tweet_id, text="Gol Timnas", screen_name="pssi"):
    return {
        "rest_id": tweet_id,
        "legacy": {
            "full_text": text,
            "created_at": "Fri Oct 09 13:13:40 +0000 2026",
            "favorite_count": 10,
            "retweet_count": 2,
            "reply_count": 1,
        },
        "core": {"user_results": {"result": {"legacy": {"screen_name": screen_name, "name": "PSSI"}}}},
    }


def _timeline_page(tweet_ids, cursor=None):
    entries = [
        {"content": {"itemContent": {"tweet_results": {"result": _tweet_payload(tid)}}}}
        for tid in tweet_ids
    ]
    if cursor:
        entries.append({"content": {"cursorType": "Bottom", "value": cursor}})
    return {"data": {"user": {"result": {"timeline": {"timeline": {"instructions": [{"entries": entries}]}}}}}}


class TweetNormalizerTests(unittest.TestCase):
    def test_reads_screen_name_from_core_block(self):
        from processors.tweet_normalizer import normalize_tweet

        payload = {
            "rest_id": "1",
            "legacy": {"full_text": "x", "created_at": "Fri Oct 09 13:13:40 +0000 2026"},
            "core": {"user_results": {"result": {"core": {"screen_name": "PSSI", "name": "PSSI"}}}},
        }
        record = normalize_tweet(payload, "graphql_api")
        self.assertEqual(record["username"], "PSSI")
        self.assertEqual(record["author"], "PSSI")
        self.assertEqual(record["url"], "https://x.com/PSSI/status/1")

    def test_falls_back_to_legacy_screen_name(self):
        from processors.tweet_normalizer import normalize_tweet

        payload = {
            "rest_id": "2",
            "legacy": {"full_text": "x"},
            "core": {"user_results": {"result": {"legacy": {"screen_name": "PBSI", "name": "PBSI"}}}},
        }
        self.assertEqual(normalize_tweet(payload, "graphql_api")["username"], "PBSI")


class TwitterPaginationTests(unittest.TestCase):
    def setUp(self):
        self.client = TwitterGraphQLBackend(auth_token="a", ct0="c")

    def test_user_timeline_follows_bottom_cursor(self):
        pages = [_timeline_page(["1"], cursor="CURSOR_2"), _timeline_page(["2"])]
        calls = []

        def fake_execute(operation, params, retried=False):
            calls.append(params["variables"])
            return pages[len(calls) - 1]

        with patch.object(self.client, "get_user_id", return_value="42"), \
             patch.object(self.client, "_execute", side_effect=fake_execute):
            tweets = self.client.get_user_tweets("pssi", limit=10)

        self.assertEqual([t["tweet_id"] for t in tweets], ["1", "2"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("cursor", calls[0])
        self.assertEqual(calls[1]["cursor"], "CURSOR_2")

    def test_timeline_stops_at_limit(self):
        pages = [_timeline_page(["1", "2", "3"], cursor="CURSOR_2")]
        calls = []

        def fake_execute(operation, params, retried=False):
            calls.append(params["variables"])
            return pages[0]

        with patch.object(self.client, "get_user_id", return_value="42"), \
             patch.object(self.client, "_execute", side_effect=fake_execute):
            tweets = self.client.get_user_tweets("pssi", limit=2)

        self.assertEqual(len(tweets), 2)
        self.assertEqual(len(calls), 1)

    def test_rate_limit_sleeps_then_retries(self):
        ok = MagicMock()
        ok.read.return_value = json.dumps(_timeline_page(["7"])).encode("utf-8")
        ok.__enter__.return_value = ok
        error = urllib.error.HTTPError(
            url="https://x.com/i/api/graphql/x/UserTweets",
            code=429,
            msg="Too Many Requests",
            hdrs={"x-rate-limit-reset": str(int(time.time()) + 7)},
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=[error, ok]), \
             patch("backends.twitter_graphql.time.sleep") as sleep_mock:
            result = self.client._execute("UserTweets", {"variables": {}})

        self.assertIsNotNone(result)
        sleep_mock.assert_called_once()
        self.assertGreater(sleep_mock.call_args[0][0], 0)

    def test_rate_limit_gives_up_after_one_retry(self):
        error = urllib.error.HTTPError(
            url="https://x.com/i/api/graphql/x/UserTweets",
            code=429,
            msg="Too Many Requests",
            hdrs={"x-rate-limit-reset": str(int(time.time()) + 7)},
            fp=None,
        )

        with patch("urllib.request.urlopen", side_effect=error), \
             patch("backends.twitter_graphql.time.sleep"):
            self.assertIsNone(self.client._execute("UserTweets", {"variables": {}}))


class TweetByIdTests(unittest.TestCase):
    def setUp(self):
        init_patcher = patch.object(TwitterChannel, "_init_session", return_value=None)
        init_patcher.start()
        self.addCleanup(init_patcher.stop)

    def test_fetch_tweet_by_id_returns_requested_tweet(self):
        response = MagicMock()
        response.read.return_value = json.dumps(_tweet_payload("999")).encode("utf-8")
        response.__enter__.return_value = response
        channel = TwitterChannel()

        with patch("urllib.request.urlopen", return_value=response):
            record = channel._fetch_tweet_by_id("999", sport="football")

        self.assertIsNotNone(record)
        self.assertEqual(record["tweet_id"], "999")
        self.assertEqual(record["sport"], "football")
        self.assertEqual(record["published_date"], "2026-10-09T13:13:40Z")

    def test_extract_prefers_direct_lookup_over_timeline(self):
        channel = TwitterChannel()
        direct = {"tweet_id": "999", "backend": "syndication_api"}
        timeline = [{"tweet_id": "111", "backend": "graphql_api"}]

        with patch.object(channel, "_fetch_tweet_by_id", return_value=direct), \
             patch.object(channel, "get_user_tweets", return_value=timeline):
            result = channel.extract("https://x.com/pssi/status/999")

        self.assertEqual(result["status"], "success")
        self.assertEqual(result["tweet"]["tweet_id"], "999")

    def test_extract_falls_back_to_timeline_match(self):
        channel = TwitterChannel()
        timeline = [{"tweet_id": "111", "backend": "graphql_api"}, {"tweet_id": "999", "backend": "graphql_api"}]

        with patch.object(channel, "_fetch_tweet_by_id", return_value=None), \
             patch.object(channel, "get_user_tweets", return_value=timeline):
            result = channel.extract("https://x.com/pssi/status/999")

        self.assertEqual(result["tweet"]["tweet_id"], "999")


if __name__ == "__main__":
    unittest.main()