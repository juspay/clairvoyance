"""The grant worker: makes the lead row of a workflow call once it has a line.

``match`` reserved a line for a call that has no lead row yet and put its entry on
``bb:grants``. This worker asks the CRM to make the lead (``materialize_call``: the run
as it is now) and then publishes the ticket, or gives the line back when the CRM says no.
An entry lost here (a crash, ``Refusal.ERROR``, a Redis error) keeps its lease, and the
lease reaper sends it again (``scripts.regrant``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Union

from app.ai.voice.agents.breeze_buddy.dispatch.queue import schedule_lead
from app.ai.voice.agents.breeze_buddy.dispatch.v2 import keys as k, scripts
from app.ai.voice.agents.breeze_buddy.dispatch.v2.latch import REFRESH_S, v2_seen
from app.ai.voice.agents.breeze_buddy.dispatch.v2.redis_client import v2_redis
from app.core.config import static
from app.core.logger import logger
from app.crm.outreach.contracts import Refusal, materialize_call

# How many entries are taken and worked at once (each is a few DB statements).
BATCH = int(getattr(static, "BB_V2_GRANT_BATCH", 16))
_POP_TIMEOUT_S = 1  # so stop() is seen within a second
_ERROR_PAUSE_S = 1.0

Materialize = Callable[[str, str, str], Awaitable[Union[str, Refusal]]]


class GrantWorker:
    def __init__(self, materialize: Materialize = materialize_call) -> None:
        self._materialize = materialize
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="bb-v2-grants")

    async def stop(self) -> None:
        """Stop taking entries; the batch in hand is finished first."""
        self._stopping.set()
        if self._task is not None:
            await self._task

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
                logger.error(f"v2 grant worker error: {type(e).__name__}: {e}")
                await self._pause(_ERROR_PAUSE_S)

    async def _round(self) -> None:
        if not await v2_seen():
            await self._pause(REFRESH_S)
            return
        # redis-py types each command as Awaitable | value; the async client awaits
        c: Any = await v2_redis()
        popped = await c.blpop([k.GRANTS_KEY], timeout=_POP_TIMEOUT_S)
        if not popped:
            return
        raws = [popped[1]] + (await c.lpop(k.GRANTS_KEY, BATCH - 1) or [])
        await asyncio.gather(*(self._grant(raw) for raw in raws))

    async def _grant(self, raw: str) -> None:
        try:
            grant = scripts.parse_grant(raw)
            if grant is None:
                logger.error(f"v2 grant worker: dropped an unreadable entry {raw!r}")
                return
            t, run_id = grant
            made = await self._materialize(run_id, t.lead_id, t.number_id)
            if made is Refusal.ERROR:
                return  # the lease stays; the reaper sends the entry again
            if isinstance(made, Refusal):
                # no call: the member leaves its room, then the line goes to the next
                # one. In this order, and only after the first is done: cut short,
                # what is left is the lease, which the reaper sends again or frees
                # (regrant); a bb:qi field left with no member and no lease would
                # stay for ever and refuse the number's hand-back
                if await scripts.withdraw(t.template_id, t.lead_id) is not None:
                    await scripts.reap_lease(t.number_id, t.lead_id, t.tk, "", 0)
            elif await scripts.publish(t.number_id, t.lead_id, t.tk) == 0:
                # the line was taken back meanwhile; the lead has a row now, so it
                # waits like any lead (left alone if it is already in its room)
                await schedule_lead(
                    made,
                    datetime.now(timezone.utc),
                    template_id=t.template_id,
                    only_if_absent=True,
                )
        except Exception as e:  # noqa: BLE001 — one entry must not stop the batch
            logger.error(f"v2 grant of {raw!r} failed: {type(e).__name__}: {e}")


_worker: Optional[GrantWorker] = None


async def start_grant_worker() -> GrantWorker:
    """Start this process's grant worker (idempotent). It idles until v2 is used."""
    global _worker
    if _worker is None:
        _worker = GrantWorker()
        _worker.start()
        logger.info("Started the v2 grant worker")
    return _worker


async def stop_grant_worker() -> None:
    if _worker is not None:
        await _worker.stop()
