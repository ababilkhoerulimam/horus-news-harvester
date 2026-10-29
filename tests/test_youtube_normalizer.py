"""Tests for the YouTube record normalizer and reply page parser."""

import unittest

from channels.social_youtube import YouTubeChannel
from processors.youtube_normalizer import build_comment, build_video_record


class TestBuildComment(unittest.TestCase):
    """Verifies comment records carry numeric likes and ISO timestamps."""

    def test_likes_are_integers(self):
        record = build_comment('c1', 'Budi', 'Halo', '1,2 rb', '')
        self.assertEqual(record['likes'], 1200)
        self.assertIsInstance(record['likes'], int)

    def test_relative_timestamp_becomes_iso(self):
        record = build_comment('c1', 'Budi', 'Halo', '5', '4 minggu yang lalu')
        self.assertRegex(record['timestamp'], r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$')

    def test_missing_author_falls_back(self):
        self.assertEqual(build_comment('c1', None, 'x', '0', '')['author'], 'Anonymous')


class TestBuildVideoRecord(unittest.TestCase):
    def test_comment_count_matches_list(self):
        comments = [build_comment('c1', 'a', 'x', '1', ''), build_comment('c2', 'b', 'y', '2', '')]
        record = build_video_record('v1', 'https://youtu.be/v1', 'Judul', 'body', comments, 'innertube_api')
        self.assertEqual(record['comment_count'], 2)
        self.assertEqual(record['status'], 'success')


class TestReplyPageParser(unittest.TestCase):
    """Verifies replies parse from the modern payload and expose follow-up tokens."""

    def setUp(self):
        self.channel = YouTubeChannel()

    def test_modern_reply_payload(self):
        page = {
            'frameworkUpdates': {'entityBatchUpdate': {'mutations': [
                {'payload': {'commentEntityPayload': {
                    'properties': {'commentId': 'r1', 'content': {'content': 'Balasan'}, 'publishedTime': ''},
                    'author': {'displayName': 'Sari'},
                    'toolbar': {'likeCountNotliked': '7'},
                }}},
            ]}},
            'onResponseReceivedEndpoints': [],
        }
        records, follow = self.channel._parse_reply_page(page, set(), 'root1', 1, 10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['parent_id'], 'root1')
        self.assertEqual(records[0]['depth'], 1)
        self.assertEqual(records[0]['likes'], 7)
        self.assertEqual(follow, [])

    def test_legacy_reply_payload_exposes_continuation(self):
        page = {
            'onResponseReceivedEndpoints': [{'appendContinuationItemsAction': {'continuationItems': [
                {'commentRenderer': {
                    'commentId': 'r2',
                    'contentText': {'runs': [{'text': 'Balasan lama'}]},
                    'authorText': {'simpleText': 'Joko'},
                    'voteCount': {'simpleText': '3'},
                    'publishedTimeText': {'simpleText': '2 jam lalu'},
                }},
                {'continuationItemRenderer': {'continuationEndpoint': {'continuationCommand': {'token': 'next-token'}}}},
            ]}}],
        }
        records, follow = self.channel._parse_reply_page(page, set(), 'root1', 1, 10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['likes'], 3)
        self.assertEqual(follow, ['next-token'])

    def test_duplicate_ids_are_skipped(self):
        page = {'onResponseReceivedEndpoints': [{'appendContinuationItemsAction': {'continuationItems': [
            {'commentRenderer': {'commentId': 'r3', 'contentText': {'runs': []}, 'authorText': {}, 'voteCount': {}, 'publishedTimeText': {}}},
        ]}}]}
        seen = {'r3'}
        records, _ = self.channel._parse_reply_page(page, seen, 'root1', 1, 10)
        self.assertEqual(records, [])


if __name__ == "__main__":
    unittest.main()
