"""Live Inbox: wake-ups over Postgres LISTEN/NOTIFY.

A writer NOTIFYs inside its own atom (``wake(..., txn)``), so the signal is
delivered only when the write commits — never for a write that rolled back.
Each API process holds ONE listening connection however many teammates are
watching, and fans each signal out to the SSE streams of that merchant.

Wake-ups only: a stream says "thread X changed (kind)", and the console
re-reads that thread or its tail. Nothing is replayed, so a stream that
drops just reconnects and re-reads its view.
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
#: Wake-ups waiting for one slow stream before it starts dropping them (a
#: dropped wake-up costs that console a refresh, nothing else).
QUEUE_DEPTH = 100
#: How long to wait before listening again after the connection failed.
RELISTEN_SECONDS = 2.0


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
                pass

    async def _listen_forever(self) -> None:
        # Re-checked after every exit from the listening block: a stream that
        # subscribed while the connection was being let go is picked up here,
        # and the task ends only with nobody watching and no await left.
        while self._streams:
            try:
                async with notify_accessor.listening(self._deliver):
                    while self._streams:
                        await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.opt(exception=e).warning(
                    "inbox listener dropped; listening again"
                )
                await asyncio.sleep(RELISTEN_SECONDS)

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
    ({"id", "kind"}) and a comment ping when quiet. Ends when the client
    goes away."""
    queue = _hub.subscribe(merchant_id)
    try:
        yield _frame("ready", {})
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
            except asyncio.TimeoutError:
                yield ": ping\n\n"
                continue
            yield _frame("thread", {"id": payload.get("t"), "kind": payload.get("k")})
    finally:
        _hub.unsubscribe(merchant_id, queue)
