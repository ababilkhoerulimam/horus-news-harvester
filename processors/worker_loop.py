"""Batch dispatch loop shared by the backfill and topic harvest workers."""

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Sequence
from urllib.parse import urlparse

from processors.crawl_frontier import interleave_by_portal
from processors.record_writer import JsonlSink

logger = logging.getLogger("worker_loop")

ProcessItem = Callable[[Dict[str, Any], JsonlSink], Awaitable[None]]


async def run_async_worker(
    worker_id: int,
    frontier,
    output_part_file: str,
    process_item: ProcessItem,
    concurrency: int,
    limit_per_worker: int,
    label: str,
    portal_filter: Optional[str] = None,
    interleave: bool = True,
    startup_stagger: float = 0.0,
    slow_domains: Sequence[str] = (),
    slow_domain_concurrency: int = 2,
    slow_domain_delay: float = 0.08,
    claim: Optional[Callable[[int], list]] = None,
) -> int:
    """Claims frontier batches and dispatches them over an async worker pool.

    process_item receives one candidate and the sink, and is responsible for
    fetching it and emitting either a record or a failure. The loop owns batch
    claiming, portal interleaving, per-domain throttling, and final flush.
    """
    part_path = Path(output_part_file)
    part_path.parent.mkdir(parents=True, exist_ok=True)

    with open(part_path, "a", encoding="utf-8") as out_f:
        sink = JsonlSink(out_f, frontier, label=label, output_name=part_path.name)
        domain_semaphores: Dict[str, asyncio.Semaphore] = {}

        async def dispatch(item: Dict[str, Any]) -> None:
            if not slow_domains:
                await process_item(item, sink)
                return
            domain = urlparse(item.get("url", "")).netloc.replace("www.", "").lower()
            if not any(s in domain for s in slow_domains):
                await process_item(item, sink)
                return
            if domain not in domain_semaphores:
                domain_semaphores[domain] = asyncio.Semaphore(slow_domain_concurrency)
            async with domain_semaphores[domain]:
                await process_item(item, sink)
                await asyncio.sleep(slow_domain_delay)

        async def consume(queue: asyncio.Queue) -> None:
            while True:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                await dispatch(item)
                queue.task_done()

        while sink.count < limit_per_worker:
            if startup_stagger and worker_id > 0:
                await asyncio.sleep(startup_stagger * worker_id)

            if claim is not None:
                batch = claim(concurrency * 4)
            else:
                batch = frontier.claim_pending_batch(limit=concurrency * 4, portal=portal_filter)
            if not batch:
                break
            if interleave and not portal_filter:
                batch = interleave_by_portal(batch)

            queue: asyncio.Queue = asyncio.Queue()
            for item in batch:
                queue.put_nowait(item)

            await asyncio.gather(*[asyncio.create_task(consume(queue)) for _ in range(concurrency)])
            await sink.flush()
            logger.info("%s Batch completed | Extracted: %s articles", label, sink.count)

        return sink.count
