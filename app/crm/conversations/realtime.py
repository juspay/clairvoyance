"""Live Inbox: wake-ups over Postgres LISTEN/NOTIFY.

A writer NOTIFYs inside its own atom (``wake(..., txn)``), so the signal is
delivered only when the write commits — never for a write that rolled back.
Each API process holds ONE listening connection however many teammates are
watching, and fans each signal out to the SSE streams of that merchant.

Wake-ups only: a stream says "thread X changed (kind)", and the console
re-reads that thread or its tail. Nothing is replayed, so a stream that
drops just reconnects and re-reads its view. Where wake-ups may have been
missed while the stream stayed open (the hub listening again, a stream too
slow to keep up, a change to many threads at once) the stream sends another
``ready``, and the console re-reads its view the same way.
"""

import asyncio
import json
from typing import Any, AsyncIterator, Dict, Optional, Set

from app.core.logger import logger
from app.crm.conversations.db import DbTxn
from app.crm.conversations.db.accessors import notify as notify_accessor

#: A quiet stream still sends a comment this often, so proxies keep it open
#: and a dead client is noticed.
PING_SECONDS = 20.0
#: Wake-ups waiting for one slow stream before they are traded for a single
#: resync (that console re-reads its view instead, nothing else is lost).
QUEUE_DEPTH = 100
#: How long to wait before listening again after the connection failed.
RELISTEN_SECONDS = 2.0
#: How often the listening connection is probed, and how long a probe may
#: take. A dropped connection reports itself; a half-open one only fails a
#: probe — without it, every Inbox on the pod would go quiet unnoticed.
PROBE_SECONDS = 15.0
PROBE_TIMEOUT = 5.0
#: How often the hub looks at its streams and its connection.
TICK_SECONDS = 1.0


#: Queued for a stream that may have missed wake-ups. Like any wake-up about
#: no one thread, it goes out as another ``ready``: "re-read your view".
RESYNC: Dict[str, Any] = {"t": None, "k": "resync"}


class ListenerLost(ConnectionError):
    """The LISTEN connection went away; listen again on a fresh one."""


def _resync(queue: "asyncio.Queue[Dict[str, Any]]") -> None:
    """Swap whatever the stream has queued for one resync: the re-read it
    triggers covers every wake-up it replaces."""
    while not queue.empty():
        queue.get_nowait()
    queue.put_nowait(RESYNC)


async def wake(
    merchant_id: str,
    thread_id: Optional[str],
    kind: str,
    txn: Optional[DbTxn] = None,
) -> None:
    """Tell the merchant's open Inboxes that a thread changed. Inside an
    atom, pass its ``txn``: the wake-up then shares the write's fate."""
    await notify_accessor.notify({"m": merchant_id, "t": thread_id, "k": kind}, txn)


class _Hub:
    """One per process: the listening connection and who is watching."""

    def __init__(self) -> None:
        self._streams: Dict[str, Set["asyncio.Queue[Dict[str, Any]]"]] = {}
        self._task: Optional[asyncio.Task] = None

    def _deliver(self, payload: Dict[str, Any]) -> None:
        merchant_id = payload.get("m")
        for queue in self._streams.get(str(merchant_id), ()):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                _resync(queue)

    def _resync_all(self) -> None:
        for queues in self._streams.values():
            for queue in queues:
                _resync(queue)

    async def _listen_forever(self) -> None:
        # Re-checked after every exit from the listening block: a stream that
        # subscribed while the connection was being let go is picked up here,
        # and the task ends only with nobody watching and no await left.
        while self._streams:
            try:
                async with notify_accessor.listening(self._deliver) as link:
                    # Live from here. Whatever committed before — while the
                    # last connection was down, or before a new stream's
                    # first read — every open console re-reads now.
                    self._resync_all()
                    await self._hold(link)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.opt(exception=e).warning(
                    "inbox listener dropped; listening again"
                )
                await asyncio.sleep(RELISTEN_SECONDS)

    async def _hold(self, link: notify_accessor.Listener) -> None:
        """Keep the connection while anyone watches; raise once it is lost
        or stops answering, so the caller listens again."""
        since_probe = 0.0
        while self._streams:
            if link.lost.is_set():
                raise ListenerLost("inbox listener connection was lost")
            await asyncio.sleep(TICK_SECONDS)
            since_probe += TICK_SECONDS
            if since_probe >= PROBE_SECONDS:
                since_probe = 0.0
                await link.probe(PROBE_TIMEOUT)

    def subscribe(self, merchant_id: str) -> "asyncio.Queue[Dict[str, Any]]":
        queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._streams.setdefault(merchant_id, set()).add(queue)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._listen_forever(), name="crm-inbox-listen"
            )
        return queue

    def unsubscribe(
        self, merchant_id: str, queue: "asyncio.Queue[Dict[str, Any]]"
    ) -> None:
        streams = self._streams.get(merchant_id)
        if streams is None:
            return
        streams.discard(queue)
        if not streams:
            del self._streams[merchant_id]


_hub = _Hub()


def _frame(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


async def stream(merchant_id: str) -> AsyncIterator[str]:
    """The merchant's wake-ups as Server-Sent Events: ``thread`` events
    ({"id", "kind"}), another ``ready`` whenever the view must be re-read
    whole, and a comment ping when quiet. Ends when the client goes away."""
    queue = _hub.subscribe(merchant_id)
    try:
        yield _frame("ready", {})
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            if payload.get("t") is None:  # many threads, or missed wake-ups
                yield _frame("ready", {})
                continue
            yield _frame("thread", {"id": payload.get("t"), "kind": payload.get("k")})
    finally:
        _hub.unsubscribe(merchant_id, queue)
