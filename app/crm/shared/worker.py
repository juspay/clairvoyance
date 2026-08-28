"""The shared drain-loop scaffold: poll cadence, jittered backoff, per-row
error isolation, heartbeat, graceful shutdown. It never learns what a row
means — the claim query and handling logic stay in each owning module."""

import asyncio
import random
import time
from typing import Awaitable, Callable, List, TypeVar

from app.core.config.static import CRM_WORKER_HEARTBEAT
from app.core.logger import logger

T = TypeVar("T")


async def run_drain_loop(
    claim: Callable[[int], Awaitable[List[T]]],
    handle: Callable[[T], Awaitable[None]],
    *,
    interval: float,
    batch: int,
    stop_event: asyncio.Event,
    name: str,
) -> None:
    """``claim`` returns the rows this iteration found (a txn-style claim has
    already committed them); ``handle`` is a per-row post-commit hook."""
    backoff = interval
    since_beat = 0
    last_beat = time.monotonic()
    while not stop_event.is_set():
        now = time.monotonic()
        # A silent worker and a dead one look identical in logs; this beat
        # is what tells them apart, so it fires on an idle loop too.
        if now - last_beat >= CRM_WORKER_HEARTBEAT:
            logger.info(f"{name}: alive, {since_beat} rows since last heartbeat")
            since_beat = 0
            last_beat = now

        try:
            rows = await claim(batch)
        except Exception as e:
            logger.error(f"{name}: claim failed: {e}")
            await _jittered_wait(backoff, stop_event)
            backoff = min(backoff * 2, 5.0)
            continue

        if not rows:
            await _jittered_wait(backoff, stop_event)
            backoff = min(backoff * 2, 5.0)
            continue

        backoff = interval
        for row in rows:
            if stop_event.is_set():
                break
            since_beat += 1
            try:
                await handle(row)
            except Exception as e:
                logger.error(f"{name}: row failed, skipping: {e}")
        # full batch -> loop again immediately (no sleep)


async def _jittered_wait(base: float, stop_event: asyncio.Event) -> None:
    delay = max(0.0, base * (1 + random.uniform(-0.2, 0.2)))
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        pass
