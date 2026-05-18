"""
Promoter — moves due leads from ``bb:schedule:leads`` to ``bb:ready:leads``.

One promoter task runs in every pod; only the leader acts (see ``leader.py``).
Tick cadence: ``BB_PROMOTER_TICK_MS`` (default 200ms).

Atomicity: the ``ZRANGEBYSCORE`` + ``ZREM`` + ``LPUSH`` move is wrapped in a
single Lua script so a mid-loop Redis hiccup can't strip a lead from the
schedule without also adding it to the ready list. Without Lua, every
promoter-pod restart could orphan whichever leads were mid-loop until the
60s reconciler heals them; the Lua wrapping eliminates that window. Pure
atomicity guarantee, no logic change.
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.keys import (
    PROMOTER_PAUSED,
    READY_LIST,
    SCHEDULE_ZSET,
)
from app.ai.voice.agents.breeze_buddy.dispatch.leader import LeaderElection
from app.core.config.static import (
    BB_PROMOTER_BATCH,
    BB_PROMOTER_TICK_MS,
)
from app.core.logger import logger
from app.services.redis import get_redis_service

# Lua script: claim up to ARGV[2] members with score <= ARGV[1], LPUSH them
# to KEYS[2], return count moved. Atomic per Redis-execution semantics.
_PROMOTE_LUA = """
local ids = redis.call('ZRANGEBYSCORE', KEYS[1], 0, ARGV[1], 'LIMIT', 0, tonumber(ARGV[2]))
if #ids == 0 then return 0 end
local moved = 0
for i = 1, #ids do
  if redis.call('ZREM', KEYS[1], ids[i]) == 1 then
    redis.call('LPUSH', KEYS[2], ids[i])
    moved = moved + 1
  end
end
return moved
"""


class Promoter:
    """
    Leader-elected promoter. Stops/starts via ``start()`` / ``stop()``.
    """

    def __init__(self, leader: Optional[LeaderElection] = None):
        self._leader = leader or LeaderElection()
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()
        self._tick_interval_s = BB_PROMOTER_TICK_MS / 1000.0

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        await self._leader.start()
        self._task = asyncio.create_task(self._loop(), name="bb-promoter")
        logger.info("Promoter started")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None
        await self._leader.stop()
        logger.info("Promoter stopped")

    @property
    def is_leader(self) -> bool:
        return self._leader.is_leader

    async def _is_paused(self) -> bool:
        try:
            redis = await get_redis_service()
            return await redis.exists(PROMOTER_PAUSED)
        except Exception:  # noqa: BLE001
            return False

    async def _tick_once(self) -> int:
        """Run one promotion. Returns the number of leads moved."""
        if not self._leader.is_leader:
            return 0
        if await self._is_paused():
            return 0
        now_ms = int(time.time() * 1000)
        try:
            redis = await get_redis_service()
            moved = await redis.run_script(
                _PROMOTE_LUA,
                keys=[SCHEDULE_ZSET, READY_LIST],
                args=[str(now_ms), str(BB_PROMOTER_BATCH)],
            )
            return int(moved) if moved else 0
        except Exception as e:  # noqa: BLE001
            logger.error(f"Promoter tick failed: {e}")
            return 0

    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                moved = await self._tick_once()
                if moved:
                    logger.debug(f"Promoter moved {moved} leads")
            except Exception as e:  # noqa: BLE001
                logger.error(f"Promoter loop unexpected error: {e}")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._tick_interval_s
                )
            except asyncio.TimeoutError:
                pass


_promoter_singleton: Optional[Promoter] = None


async def start_promoter() -> Promoter:
    """Start the global promoter task. Idempotent."""
    global _promoter_singleton
    if _promoter_singleton is None:
        _promoter_singleton = Promoter()
        await _promoter_singleton.start()
    return _promoter_singleton


async def stop_promoter() -> None:
    """Stop the global promoter task. Safe to call without a prior start."""
    global _promoter_singleton
    if _promoter_singleton is not None:
        await _promoter_singleton.stop()
        _promoter_singleton = None
