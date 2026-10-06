"""The v2 acceptor (spec 2026-10-05 §4.1-4.3, design card §4).

One per dialler pod. It blocks on ``bb:tickets`` (every ticket ``match`` issues, in issue
order), takes up to BB_V2_ACCEPT_BATCH entries per round trip and runs one ``dial_ticket``
coroutine per ticket. There is no pool: a coroutine waiting on a slow step costs memory
only, so a slow merchant holds only its own lines (with the fixed pool this replaces, one
merchant's hung TTS parked almost every task of a pod). ``claim`` makes a ticket delivered
twice (a reaper re-push racing the first delivery) dial once.

The kill switch is read once per batch, right after the pop: a batch popped while it is
off goes back to the head of the list, and a stale read is bounded by one batch.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Dict, List, Optional

import aiohttp

from app.ai.voice.agents.breeze_buddy.dispatch.queue import schedule_lead
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import REFRESH_S, v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.redis_client import v2_redis
from app.ai.voice.agents.breeze_buddy.dispatch.v2.scripts import Ticket, parse_ticket
from app.core.concurrency import spawn_background_task
from app.core.config import dynamic as dyn_cfg
from app.core.config.static import (
    BB_V2_ACCEPT_BATCH,
    BB_V2_ACCEPT_DISABLED_SLEEP_S,
    BB_V2_ACCEPT_ERROR_BACKOFF_S,
    BB_V2_ACCEPT_FULL_SLEEP_S,
    BB_V2_MAX_INFLIGHT_PER_POD,
    BB_V2_TICKET_BLPOP_TIMEOUT_S,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session

if TYPE_CHECKING:
    from app.ai.voice.agents.breeze_buddy.dispatch.worker import Worker

_FULL_LOG_EVERY_S = 30.0  # the "pod full" warning, at most this often

DialFn = Callable[
    [Ticket, Optional[aiohttp.ClientSession], "Worker", asyncio.Event],
    Coroutine[Any, Any, bool],
]


async def _requeue(ticket: Ticket) -> None:
    """The lead back in its room, due now (its line was given back first: rule 17)."""
    await schedule_lead(
        ticket.lead_id,
        datetime.now(timezone.utc),
        jitter_ms=0,
        template_id=ticket.template_id,
    )


async def dial_ticket(
    ticket: Ticket,
    session: Optional[aiohttp.ClientSession],
    worker: Worker,
    stopping: asyncio.Event,
) -> bool:
    """Claim the ticket, then run today's checks and the dial on its held line. True
    only if a call was placed. Every exit leaves the line with the call or gives it
    back, both conditional on the ticket id and our owner (rule 15)."""
    # lazy: the worker imports managers.calls, which imports the dispatch package
    from app.ai.voice.agents.breeze_buddy.dispatch.worker import ClaimedTicket

    owner = uuid.uuid4().hex
    if not await scripts.claim(ticket.number_id, ticket.lead_id, ticket.tk, owner):
        return False  # void (reaped, re-issued, handed back) or delivered twice
    if stopping.is_set():
        await scripts.return_line(ticket.number_id, ticket.lead_id, ticket.tk, owner)
        await _requeue(ticket)
        return False
    dialled = False
    try:
        dialled = await worker._dispatch(
            ticket.lead_id,
            session,
            held=ClaimedTicket(ticket.number_id, ticket.tk, owner, stopping),
        )
    except asyncio.CancelledError:
        # A shutdown cancelled the dispatch before its commit point, and _dispatch gave
        # the line back; make sure, then put the lead back in its room now rather than
        # after the 60 s backlog job. A line marked dialling stays with its call.
        given = await asyncio.shield(
            scripts.return_line(ticket.number_id, ticket.lead_id, ticket.tk, owner)
        )
        if given != scripts.GiveBack.DIALLING:
            await asyncio.shield(_requeue(ticket))
        raise
    except Exception as e:  # noqa: BLE001 — the line is settled below either way
        logger.error(
            f"v2 dial of lead {ticket.lead_id} (ticket {ticket.tk} on {ticket.number_id}) failed: {e}"
        )
    if dialled:
        await scripts.clear_lease(ticket.number_id, ticket.lead_id, ticket.tk, owner)
    else:
        await scripts.return_line(ticket.number_id, ticket.lead_id, ticket.tk, owner)
    return dialled


class Acceptor:
    def __init__(self, dial: DialFn = dial_ticket) -> None:
        # lazy: see dial_ticket
        from app.ai.voice.agents.breeze_buddy.dispatch.worker import Worker

        self._worker_cls = Worker
        self._dial = dial
        # set by stop(); every held line carries it
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task] = None
        self._session: Optional[aiohttp.ClientSession] = None
        # one Worker per coroutine: _dispatch keeps per-dispatch state on it
        self._inflight: Dict[asyncio.Task, Worker] = {}
        self._cancelled: set[asyncio.Task] = set()
        self._full_logged = float("-inf")

    @property
    def running(self) -> bool:
        return (
            self._task is not None
            and not self._task.done()
            and not self._stopping.is_set()
        )

    @property
    def in_flight(self) -> int:
        return len(self._inflight)

    def start(self) -> None:
        # Every dispatch's merchant pre-check goes through this one session: aiohttp's
        # default of 100 connections would queue the 101st dispatch behind slow ones,
        # a hidden pool again. Each request carries its own timeout.
        self._session = create_aiohttp_session(connector=aiohttp.TCPConnector(limit=0))
        self._task = asyncio.create_task(self._loop(), name="bb-v2-acceptor")

    async def _pause(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self._round()
            except Exception as e:  # noqa: BLE001 — a Redis blip must not end it
                logger.error(f"v2 acceptor error: {type(e).__name__}: {e}")
                await self._pause(BB_V2_ACCEPT_ERROR_BACKOFF_S)

    async def _round(self) -> None:
        if not await v2_seen():
            # v2 never used on this pod: no BLPOP, no config read (rule 23); look again
            # when the latch would
            await self._pause(REFRESH_S)
            return
        room = BB_V2_MAX_INFLIGHT_PER_POD - len(self._inflight)
        if room <= 0:
            self._log_full()
            await self._pause(BB_V2_ACCEPT_FULL_SLEEP_S)
            return
        # redis-py types each command as Awaitable | value; the async client awaits
        c: Any = await v2_redis()
        popped = await c.blpop([k.TICKETS_KEY], timeout=BB_V2_TICKET_BLPOP_TIMEOUT_S)
        if not popped:
            return
        raws: List[str] = [popped[1]]
        more = min(BB_V2_ACCEPT_BATCH, room) - 1
        if more > 0:
            raws += await c.lpop(k.TICKETS_KEY, more) or []
        if self._stopping.is_set() or not await dyn_cfg.BB_DISPATCH_ENABLED():
            # stop() came while we waited, or the kill switch is on (rule 24): back to
            # the head, oldest first, untouched
            await c.lpush(k.TICKETS_KEY, *reversed(raws))
            await self._pause(BB_V2_ACCEPT_DISABLED_SLEEP_S)
            return
        for raw in raws:
            ticket = parse_ticket(raw)
            if ticket is None:
                logger.error(f"v2 acceptor: dropped an unreadable ticket {raw!r}")
                continue
            self._spawn(ticket)

    def _log_full(self) -> None:
        now = time.monotonic()
        if now - self._full_logged > _FULL_LOG_EVERY_S:
            self._full_logged = now
            logger.warning(
                f"v2 acceptor: {len(self._inflight)} dials in flight "
                "(BB_V2_MAX_INFLIGHT_PER_POD); other pods take the next tickets"
            )

    def _spawn(self, ticket: Ticket) -> None:
        worker = self._worker_cls(worker_uuid=f"v2-{ticket.number_id}-{ticket.tk}")
        task = asyncio.create_task(
            self._dial(ticket, self._session, worker, self._stopping),
            name=f"bb-v2-dial-{ticket.lead_id}",
        )
        self._inflight[task] = worker
        task.add_done_callback(self._done)

    def _done(self, task: asyncio.Task) -> None:
        self._inflight.pop(task, None)
        self._cancelled.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.error(f"v2 dial coroutine failed: {task.exception()!r}")
        if self._stopping.is_set() and not self._inflight:
            # the last dial that outlived stop()'s grace: nothing can use it now
            self._close_session()

    def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            spawn_background_task(self._session.close(), name="bb-v2-acceptor-session")

    async def stop(self, grace_s: float) -> None:
        """Stop popping (entries popped meanwhile go back to the head of the list), cancel
        dispatches still in their checks (they give the line back and are re-queued) and
        wait up to ``grace_s`` for the rest. A dispatch past its commit point is never
        cancelled: still running after the grace, its lease is the reaper's and its row
        the stuck sweep's."""
        self._stopping.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + grace_s
        while True:
            for task, worker in list(self._inflight.items()):
                if task not in self._cancelled and worker.v2_cancel_safe:
                    self._cancelled.add(task)
                    task.cancel()
            pending = [task for task in self._inflight if not task.done()]
            if self._task is not None and not self._task.done():
                pending.append(self._task)
            remaining = deadline - loop.time()
            if not pending or remaining <= 0:
                break
            # re-check the phases often: a dispatch can move into a cancellable one
            await asyncio.wait(pending, timeout=min(remaining, 0.25))
        if self._inflight:
            # the session stays open for them; the last one to end closes it
            logger.error(
                f"v2 acceptor: {len(self._inflight)} dials still running after "
                f"{grace_s} s of shutdown; not cancelled (their calls may exist)"
            )
        elif self._session is not None:
            await self._session.close()


_acceptor: Optional[Acceptor] = None


async def start_acceptor() -> Acceptor:
    """Start this pod's acceptor (idempotent). It idles until v2 is first used."""
    global _acceptor
    if _acceptor is None or not _acceptor.running:
        _acceptor = Acceptor()
        _acceptor.start()
        logger.info("Started the v2 acceptor")
    return _acceptor


async def stop_acceptor(grace_s: float) -> None:
    if _acceptor is not None:
        await _acceptor.stop(grace_s)
