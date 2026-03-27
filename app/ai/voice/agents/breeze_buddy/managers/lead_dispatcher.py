"""
LeadDispatcher — event-driven lead processing orchestrator.

Replaces poll-based cron with reactive event triggers:
  1. on_lead_created: process immediately or schedule for later
  2. on_channel_freed: grab next waiting lead the moment capacity opens
  3. Delayed scheduler: wake at exact next_attempt_at time (not a fixed interval)
  4. Startup recovery: catch any leads missed during downtime

No external cron. No fixed timer. Leads are processed in response to events.
"""

import asyncio
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

import aiohttp

from app.core.config.static import BACKLOG_WORKER_COUNT
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    get_leads_based_on_status_and_next_attempt,
    grab_next_backlog_lead,
    release_lock_on_lead_by_id,
)
from app.schemas import LeadCallStatus
from app.services.redis.client import get_redis_service

# Type alias for the lead processing function injected at startup.
# Signature: async def process_fn(lead_id: str, session: aiohttp.ClientSession) -> None
ProcessFn = Callable[[str, aiohttp.ClientSession], Awaitable[None]]

# Redis key for the delayed lead sorted set
SCHEDULED_LEADS_KEY = "lead_dispatcher:scheduled_leads"


class LeadDispatcher:
    """Event-driven lead processing orchestrator.

    Instead of polling the database every N seconds, leads are processed when:
    - A new lead is created (push_lead API, _retry_call)
    - A channel is freed (call completed, call unanswered)
    - A scheduled time is reached (retry_offset, initial_offset)
    - On startup (recover any overdue leads from downtime)
    """

    def __init__(self, process_fn: ProcessFn, max_workers: int = BACKLOG_WORKER_COUNT):
        self._process_fn = process_fn
        self._max_workers = max_workers
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=max_workers * 2)
        self._workers: list[asyncio.Task] = []
        self._scheduler_task: Optional[asyncio.Task] = None
        self._schedule_event = asyncio.Event()
        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self) -> None:
        """Start the dispatcher: workers + delayed scheduler + startup recovery."""
        self._running = True
        self._session = create_aiohttp_session()

        # Start worker pool
        for i in range(self._max_workers):
            task = asyncio.create_task(self._worker(i), name=f"lead-worker-{i}")
            self._workers.append(task)

        # Start delayed lead scheduler
        self._scheduler_task = asyncio.create_task(
            self._delayed_lead_scheduler(), name="lead-scheduler"
        )

        # Recover overdue leads from DB (handles restarts, missed events)
        await self._recover_overdue_leads()

        logger.info(f"LeadDispatcher started with {self._max_workers} workers")

    async def stop(self) -> None:
        """Gracefully stop the dispatcher. Drains in-flight work."""
        logger.info("LeadDispatcher stopping...")
        self._running = False
        self._schedule_event.set()  # wake scheduler so it can exit

        # Cancel all workers and scheduler
        for w in self._workers:
            w.cancel()
        if self._scheduler_task:
            self._scheduler_task.cancel()

        all_tasks = self._workers + (
            [self._scheduler_task] if self._scheduler_task else []
        )
        await asyncio.gather(*all_tasks, return_exceptions=True)
        self._workers.clear()
        self._scheduler_task = None

        if self._session:
            await self._session.close()
            self._session = None

        logger.info("LeadDispatcher stopped")

    # --- Event Handlers ---

    async def on_lead_created(
        self, lead_id: str, next_attempt_at: Optional[datetime]
    ) -> None:
        """Called after push_lead or _retry_call creates a BACKLOG lead.

        If the lead is ready now, enqueue for immediate processing.
        If the lead has a future next_attempt_at, schedule it in the Redis sorted set.
        """
        if not self._running:
            return

        now = datetime.now(timezone.utc)
        if next_attempt_at is None or next_attempt_at <= now:
            logger.info(f"Lead {lead_id} ready for immediate processing")
            await self._try_enqueue(lead_id)
        else:
            # Schedule for later
            score = next_attempt_at.timestamp()
            try:
                redis = await get_redis_service()
                await redis.zadd(SCHEDULED_LEADS_KEY, {lead_id: score})
                self._schedule_event.set()  # wake scheduler to recalculate sleep
                logger.info(
                    f"Lead {lead_id} scheduled for {next_attempt_at.isoformat()}"
                )
            except Exception as e:
                logger.error(f"Failed to schedule lead {lead_id}: {e}")
                # Fallback: enqueue immediately (will be checked by calling hours anyway)
                await self._try_enqueue(lead_id)

    async def on_channel_freed(self) -> None:
        """Called after _release_number frees a channel.

        Finds the next eligible BACKLOG lead and enqueues it for processing.
        The lead is NOT pre-locked here — process_single_lead handles locking.
        If the queue is full, the lead stays unlocked in BACKLOG and will be
        picked up on the next on_channel_freed or startup recovery.
        """
        if not self._running:
            return

        try:
            lead = await grab_next_backlog_lead(datetime.now(timezone.utc))
            if lead:
                if self._try_enqueue(lead.id):
                    logger.info(
                        f"Channel freed → enqueued lead {lead.id} for processing"
                    )
        except Exception as e:
            logger.error(f"Error in on_channel_freed: {e}")

    # --- Internal: Worker Pool ---

    async def _worker(self, worker_id: int) -> None:
        """Single worker coroutine: pulls lead IDs from queue and processes them."""
        assert (
            self._session is not None
        ), "Dispatcher must be started before workers run"
        session = self._session

        while self._running:
            try:
                lead_id = await asyncio.wait_for(self._queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                await self._process_fn(lead_id, session)
            except Exception as e:
                logger.error(
                    f"Worker {worker_id} failed on lead {lead_id}: {e}", exc_info=True
                )
                # Ensure lock is released even if process_single_lead crashes
                try:
                    await release_lock_on_lead_by_id(lead_id)
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    async def _try_enqueue(self, lead_id: str) -> bool:
        """Try to enqueue a lead ID. Returns False if queue is full (backpressure)."""
        try:
            self._queue.put_nowait(lead_id)
            return True
        except asyncio.QueueFull:
            logger.debug(
                f"Queue full ({self._queue.qsize()}/{self._queue.maxsize}), "
                f"lead {lead_id} stays in DB for next channel_freed"
            )
            return False

    # --- Internal: Delayed Lead Scheduler ---

    async def _delayed_lead_scheduler(self) -> None:
        """Watches the Redis sorted set for leads whose next_attempt_at has arrived.

        NOT a fixed-interval poller. Sleeps until the exact next scheduled time,
        or wakes early if a new lead is scheduled (via _schedule_event).
        """
        while self._running:
            try:
                redis = await get_redis_service()
                now = time.time()

                # Check for overdue scheduled leads
                results = await redis.zrangebyscore(
                    SCHEDULED_LEADS_KEY, 0, now, start=0, num=self._max_workers
                )

                if results:
                    for lead_id in results:
                        # zrem returns 1 if we removed it, 0 if another pod got it first
                        removed = await redis.zrem(SCHEDULED_LEADS_KEY, str(lead_id))
                        if removed:
                            await self._try_enqueue(str(lead_id))
                    continue  # immediately check for more

                # No overdue leads. Find next scheduled time.
                next_items = await redis.zrangebyscore(
                    SCHEDULED_LEADS_KEY,
                    now,
                    float("inf"),
                    start=0,
                    num=1,
                    withscores=True,
                )

                if next_items:
                    # next_items is [(member, score), ...] when withscores=True
                    if isinstance(next_items[0], tuple):
                        next_time = next_items[0][1]
                    else:
                        # Flat list: [member, score]
                        next_time = (
                            float(next_items[1]) if len(next_items) > 1 else now + 60
                        )
                    delay = max(0.1, next_time - time.time())
                else:
                    delay = 60.0  # nothing scheduled, sleep and re-check

                # Wait until next scheduled time OR new lead signal
                try:
                    await asyncio.wait_for(self._schedule_event.wait(), timeout=delay)
                    self._schedule_event.clear()
                except asyncio.TimeoutError:
                    pass  # scheduled time reached, loop back

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Lead scheduler error: {e}", exc_info=True)
                await asyncio.sleep(1)  # brief pause before retry

    # --- Internal: Startup Recovery ---

    async def _recover_overdue_leads(self) -> None:
        """On startup, process any BACKLOG leads that should have been processed.

        Handles two cases:
        1. Overdue entries in the Redis sorted set (leads scheduled but pod restarted)
        2. Overdue BACKLOG leads in the DB (leads that arrived while no dispatcher was running)
        """
        recovered_scheduled = 0
        recovered_db = 0

        try:
            # 1. Drain overdue entries from Redis sorted set
            redis = await get_redis_service()
            now = time.time()
            overdue = await redis.zrangebyscore(
                SCHEDULED_LEADS_KEY, 0, now, start=0, num=1000
            )
            for lead_id in overdue:
                await redis.zrem(SCHEDULED_LEADS_KEY, str(lead_id))
                await self._try_enqueue(str(lead_id))
                recovered_scheduled += 1
        except Exception as e:
            logger.error(f"Error recovering scheduled leads: {e}")

        try:
            # 2. Check DB for any BACKLOG leads with next_attempt_at <= now
            leads = await get_leads_based_on_status_and_next_attempt(
                LeadCallStatus.BACKLOG, datetime.now(timezone.utc)
            )
            # Don't overload the queue — just enqueue up to capacity
            for lead in leads[: self._max_workers * 2]:
                await self._try_enqueue(lead.id)
                recovered_db += 1
        except Exception as e:
            logger.error(f"Error recovering DB leads: {e}")

        if recovered_scheduled or recovered_db:
            logger.info(
                f"Startup recovery: {recovered_scheduled} from Redis, "
                f"{recovered_db} from DB"
            )


# --- Singleton ---

_lead_dispatcher: Optional[LeadDispatcher] = None


def get_lead_dispatcher() -> Optional[LeadDispatcher]:
    """Get the global LeadDispatcher instance. Returns None if not started."""
    return _lead_dispatcher


async def initialize_lead_dispatcher(
    process_fn: ProcessFn, max_workers: int = BACKLOG_WORKER_COUNT
) -> LeadDispatcher:
    """Create and start the global LeadDispatcher."""
    global _lead_dispatcher
    _lead_dispatcher = LeadDispatcher(process_fn=process_fn, max_workers=max_workers)
    await _lead_dispatcher.start()
    return _lead_dispatcher


async def shutdown_lead_dispatcher() -> None:
    """Stop the global LeadDispatcher."""
    global _lead_dispatcher
    if _lead_dispatcher:
        await _lead_dispatcher.stop()
        _lead_dispatcher = None
