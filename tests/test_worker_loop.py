"""Tests for the shared batch dispatch loop."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from processors.record_writer import build_news_record
from processors.worker_loop import run_async_worker


class FakeFrontier:
    def __init__(self, items):
        self.pending = list(items)
        self.completed = []
        self.failed = []
        self.flushes = 0
        self.closed = False

    def claim_pending_batch(self, limit=50, portal=None):
        batch = self.pending[:limit]
        self.pending = self.pending[limit:]
        return batch

    def mark_completed(self, url, immediate=False):
        self.completed.append(url)

    def mark_failed(self, url, error="", immediate=False):
        self.failed.append((url, error))

    def flush(self):
        self.flushes += 1

    def close(self):
        self.closed = True


class TestRunAsyncWorker(unittest.TestCase):
    """Verifies batch claiming, record emission, and failure routing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.out = Path(self.temp_dir.name) / 'part0.jsonl'

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_emits_records_and_routes_failures(self):
        items = [
            {"url": "https://a.example.com/1"},
            {"url": "https://a.example.com/2"},
            {"url": "https://a.example.com/3"},
        ]
        frontier = FakeFrontier(items)

        async def process_item(item, sink):
            if item["url"].endswith("2"):
                await sink.fail(item["url"], "boom")
                return
            await sink.emit(build_news_record(
                url=item["url"], title="t", published_date="",
                archive_date="", content="body", backend="curl_impersonate",
            ))

        count = asyncio.run(run_async_worker(
            worker_id=0,
            frontier=frontier,
            output_part_file=str(self.out),
            process_item=process_item,
            concurrency=2,
            limit_per_worker=10,
            label="[Core-0]",
        ))

        self.assertEqual(count, 2)
        self.assertEqual(len(frontier.completed), 2)
        self.assertEqual(frontier.failed, [('https://a.example.com/2', 'boom')])
        self.assertGreaterEqual(frontier.flushes, 1)

        lines = [json.loads(line) for line in self.out.read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertEqual({line["url"] for line in lines}, {"https://a.example.com/1", "https://a.example.com/3"})

    def test_stops_when_frontier_is_empty(self):
        frontier = FakeFrontier([])

        async def process_item(item, sink):
            await sink.emit({'url': item['url']})

        count = asyncio.run(run_async_worker(
            worker_id=0,
            frontier=frontier,
            output_part_file=str(self.out),
            process_item=process_item,
            concurrency=2,
            limit_per_worker=10,
            label="[Core-0]",
        ))
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
