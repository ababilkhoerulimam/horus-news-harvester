"""Tests for the social harvester orchestration."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from processors.topic_config import (
    SocialConfig,
    TopicConfig,
    TwitterSocialConfig,
    YouTubeSocialConfig,
)
from scripts import social_harvester


class FakeYouTubeChannel:
    def __init__(self, videos, comments_per_video=2):
        self.videos = videos
        self.comments_per_video = comments_per_video
        self.extracted = []

    def search(self, query, sport='football', limit=10, config=None):
        return [
            {'video_id': vid, 'title': f'{query} {vid}', 'published_date': '2026-10-20'}
            for vid in self.videos[:limit]
        ]

    def extract(self, url, config=None):
        self.extracted.append(url)
        vid = url.rsplit('=', 1)[-1]
        return {
            'url': url,
            'video_id': vid,
            'title': f'video {vid}',
            'content': 'body',
            'comment_count': self.comments_per_video,
            'comments': [{'id': f'{vid}_c{i}', 'text': 'komentar'} for i in range(self.comments_per_video)],
            'backend': 'innertube_api',
            'status': 'success',
        }


class FakeTwitterChannel:
    def __init__(self, tweets):
        self.tweets = tweets

    def search(self, query, sport='football', limit=10, config=None):
        return [dict(t) for t in self.tweets[:limit]]

    def get_user_tweets(self, username, sport='football', limit=20, config=None):
        return [dict(t) for t in self.tweets[:limit]]


class TestSocialHarvester(unittest.TestCase):
    """Verifies discovery, dedup, and output for both platforms."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.cfg = TopicConfig.model_validate({
            'topic': {'id': 'sports', 'name': 'Sports'},
            'keywords': ['timnas'],
            'social': {
                'enabled': True,
                'youtube': {'queries': ['timnas'], 'max_videos_per_run': 5},
                'twitter': {'queries': ['timnas'], 'handles': [], 'max_tweets_per_query': 5},
            },
        })

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_youtube_harvest_writes_videos_with_comments(self):
        channel = FakeYouTubeChannel(['v1', 'v2'])
        out_dir = self.root / 'youtube'
        with patch.object(social_harvester, 'YOUTUBE_DIR', out_dir), \
             patch.object(social_harvester, 'FRONTIER_TEMPLATE', str(self.root / '{topic}_{platform}.db')), \
             patch.object(social_harvester, 'YouTubeChannel', return_value=channel), \
             patch.object(social_harvester, 'refresh_client_version', return_value='2.0'):
            res = social_harvester.harvest_youtube(self.cfg)

        self.assertEqual(res['videos'], 2)
        self.assertEqual(res['comments'], 4)
        records = [json.loads(line) for line in (out_dir / 'sports.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['sport'], 'sports')
        self.assertEqual(len(records[0]['comments']), 2)

    def test_youtube_second_run_skips_known_videos(self):
        channel = FakeYouTubeChannel(['v1'])
        out_dir = self.root / 'youtube'
        with patch.object(social_harvester, 'YOUTUBE_DIR', out_dir), \
             patch.object(social_harvester, 'FRONTIER_TEMPLATE', str(self.root / '{topic}_{platform}.db')), \
             patch.object(social_harvester, 'YouTubeChannel', return_value=channel), \
             patch.object(social_harvester, 'refresh_client_version', return_value='2.0'):
            first = social_harvester.harvest_youtube(self.cfg)
            second = social_harvester.harvest_youtube(self.cfg)

        self.assertEqual(first['videos'], 1)
        self.assertEqual(second['videos'], 0)
        self.assertEqual(len(channel.extracted), 1)

    def test_twitter_harvest_dedups_by_tweet_id(self):
        tweets = [
            {'tweet_id': '1', 'text': 'a', 'title': 't1', 'published_date': '2026-10-20'},
            {'tweet_id': '2', 'text': 'b', 'title': 't2', 'published_date': '2026-10-20'},
        ]
        out_dir = self.root / 'twitter'
        with patch.object(social_harvester, 'TWITTER_DIR', out_dir), \
             patch.object(social_harvester, 'FRONTIER_TEMPLATE', str(self.root / '{topic}_{platform}.db')), \
             patch.object(social_harvester, 'TwitterChannel', return_value=FakeTwitterChannel(tweets)):
            first = social_harvester.harvest_twitter(self.cfg)
            second = social_harvester.harvest_twitter(self.cfg)

        self.assertEqual(first['tweets'], 2)
        self.assertEqual(second['tweets'], 0)

    def test_missing_targets_are_skipped(self):
        cfg = TopicConfig.model_validate({
            'topic': {'id': 'ikn', 'name': 'IKN'},
            'keywords': ['ikn'],
            'social': {'enabled': True},
        })
        self.assertEqual(social_harvester.harvest_youtube(cfg)['status'], 'skipped')
        self.assertEqual(social_harvester.harvest_twitter(cfg)['status'], 'skipped')


class ConcurrencyProbeChannel(FakeYouTubeChannel):
    """Records how many videos are in flight at once and from which thread."""

    def __init__(self, videos, delay=0.12, fail_on=()):
        super().__init__(videos)
        self.delay = delay
        self.fail_on = set(fail_on)
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.extract_threads = []
        self.attempts = []

    def extract(self, url, config=None):
        vid = url.rsplit('=', 1)[-1]
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.extract_threads.append(threading.current_thread().name)
            self.attempts.append(vid)
        try:
            time.sleep(self.delay)
            if vid in self.fail_on:
                raise RuntimeError(f'{vid} exploded')
            return super().extract(url, config=config)
        finally:
            with self._lock:
                self._active -= 1


class SpyFrontier:
    """Frontier stand-in that records the thread of every call."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def _note(self, name):
        with self._lock:
            self.calls.append((name, threading.current_thread().name))

    def mark_in_progress(self, urls):
        self._note('mark_in_progress')

    def mark_completed(self, url):
        self._note('mark_completed')

    def mark_failed(self, url, error=''):
        self._note('mark_failed')

    def flush(self):
        self._note('flush')

    def close(self):
        self._note('close')

    def get_pending_batch(self, limit=10, portal=None):
        return []

    def add_candidates(self, candidates, portal=None):
        self._note('add_candidates')
        return len(candidates)


class TestYouTubeVideoConcurrency(unittest.TestCase):
    """Verifies videos overlap in wall clock while the frontier stays single-threaded."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _cfg(self, **youtube):
        payload = {'queries': ['timnas'], 'max_videos_per_run': 8}
        payload.update(youtube)
        return TopicConfig.model_validate({
            'topic': {'id': 'sports', 'name': 'Sports'},
            'keywords': ['timnas'],
            'social': {'enabled': True, 'youtube': payload},
        })

    def _run(self, cfg, channel, frontier=None):
        frontier = frontier or SpyFrontier()
        with patch.object(social_harvester, 'YOUTUBE_DIR', self.root / 'youtube'), \
             patch.object(social_harvester, 'FRONTIER_TEMPLATE', str(self.root / '{topic}_{platform}.db')), \
             patch.object(social_harvester, 'YouTubeChannel', return_value=channel), \
             patch.object(social_harvester, '_frontier', return_value=frontier), \
             patch.object(social_harvester, 'refresh_client_version', return_value='2.0'):
            return social_harvester.harvest_youtube(cfg), frontier

    def test_videos_overlap_when_workers_above_one(self):
        channel = ConcurrencyProbeChannel(['v1', 'v2', 'v3', 'v4'], delay=0.12)
        started = time.monotonic()
        res, _ = self._run(self._cfg(video_workers=4), channel)
        elapsed = time.monotonic() - started

        self.assertEqual(res['videos'], 4)
        self.assertGreaterEqual(channel.max_active, 2)
        # Sequential would need 4 x 0.12 = 0.48s. Anything under that proves overlap.
        self.assertLess(elapsed, 0.48)

    def test_single_worker_stays_sequential(self):
        channel = ConcurrencyProbeChannel(['v1', 'v2', 'v3'], delay=0.05)
        res, _ = self._run(self._cfg(video_workers=1), channel)

        self.assertEqual(res['videos'], 3)
        self.assertEqual(channel.max_active, 1)
        # One worker means one thread for every video, whichever thread the pool chose.
        self.assertEqual(len(set(channel.extract_threads)), 1)

    def test_frontier_is_only_touched_from_the_main_thread(self):
        channel = ConcurrencyProbeChannel(['v1', 'v2', 'v3', 'v4'], delay=0.05)
        _, frontier = self._run(self._cfg(video_workers=4), channel)

        names = {thread for _, thread in frontier.calls}
        self.assertEqual(names, {'MainThread'})
        self.assertIn('mark_in_progress', {call for call, _ in frontier.calls})
        self.assertIn('mark_completed', {call for call, _ in frontier.calls})

    def test_one_failing_video_does_not_stop_the_others(self):
        channel = ConcurrencyProbeChannel(['v1', 'v2', 'v3'], delay=0.02, fail_on={'v2'})
        res, frontier = self._run(self._cfg(video_workers=3), channel)

        self.assertEqual(res['videos'], 2)
        self.assertEqual(res['failed'], 1)
        self.assertEqual(sorted(channel.attempts), ['v1', 'v2', 'v3'])
        self.assertIn('mark_failed', {call for call, _ in frontier.calls})

    def test_video_workers_defaults_to_four(self):
        self.assertEqual(YouTubeSocialConfig().video_workers, 4)


if __name__ == "__main__":
    unittest.main()
