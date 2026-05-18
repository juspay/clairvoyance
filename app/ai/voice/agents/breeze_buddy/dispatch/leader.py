"""
Leader election for the promoter.

Single-leader semantics via ``SET key value NX EX``. The current leader
renews well before TTL; on renewal failure or shutdown the lock expires
and another pod takes over within ``BB_PROMOTER_LEADER_TTL_S``.

Safety note: a brief split-brain window during hand-off is acceptable
because the promoter's authoritative pick is ``ZREM`` (returns 0 for the
loser of any race). See ``promoter.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.ai.voice.agents.breeze_buddy.dispatch.keys import PROMOTER_LEADER
from app.core.config.static import (
    BB_PROMOTER_LEADER_RENEW_S,
    BB_PROMOTER_LEADER_TTL_S,
)
from app.core.logger import logger
from app.services.redis import get_redis_service


class LeaderElection:
    """
    Cooperative leader-election primitive.

    Usage::

        leader = LeaderElection()
        await leader.start()
        while running:
            if leader.is_leader:
                ...
            await asyncio.sleep(...)
        await leader.stop()
    """

    def __init__(self, key: str = PROMOTER_LEADER, instance_id: Optional[str] = None):
        self._key = key
        self._instance_id = instance_id or str(uuid.uuid4())
        self._is_leader = False
        self._task: Optional[asyncio.Task] = None
        self._stopping = asyncio.Event()

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    @property
    def instance_id(self) -> str:
        return self._instance_id

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="bb-promoter-leader")
        logger.info(f"LeaderElection started (instance_id={self._instance_id})")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=BB_PROMOTER_LEADER_TTL_S + 1)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._task = None

        if self._is_leader:
            await self._release()
        self._is_leader = False

    async def _try_acquire(self) -> bool:
        """SET NX EX. Returns True on success."""
        try:
            redis = await get_redis_service()
            return await redis.set(
                key=self._key,
                value=self._instance_id,
                nx=True,
                ex=BB_PROMOTER_LEADER_TTL_S,
            )
        except Exception as e:  # noqa: BLE001
            logger.error(f"LeaderElection: acquire failed: {e}")
            return False

    async def _try_renew(self) -> bool:
        """
        CAS renew — only extend the TTL if we still own the lock.

        Lua: ``if GET == self then EXPIRE; return 1 else return 0``.
        """
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "  return redis.call('EXPIRE', KEYS[1], ARGV[2]) "
            "else return 0 end"
        )
        try:
            redis = await get_redis_service()
            result = await redis.run_script(
                script,
                keys=[self._key],
                args=[self._instance_id, str(BB_PROMOTER_LEADER_TTL_S)],
            )
            return bool(result)
        except Exception as e:  # noqa: BLE001
            logger.error(f"LeaderElection: renew failed: {e}")
            return False

    async def _release(self) -> None:
        """Release the lock only if we still own it."""
        script = (
            "if redis.call('GET', KEYS[1]) == ARGV[1] then "
            "  return redis.call('DEL', KEYS[1]) "
            "else return 0 end"
        )
        try:
            redis = await get_redis_service()
            await redis.run_script(script, keys=[self._key], args=[self._instance_id])
        except Exception as e:  # noqa: BLE001
            logger.error(f"LeaderElection: release failed: {e}")

    async def _loop(self) -> None:
        """
        Background loop. Always trying — if we hold the lock we renew; if
        not, we try to acquire.
        """
        while not self._stopping.is_set():
            try:
                if self._is_leader:
                    if not await self._try_renew():
                        logger.warning(
                            f"LeaderElection: lost leadership "
                            f"(instance_id={self._instance_id})"
                        )
                        self._is_leader = False
                else:
                    if await self._try_acquire():
                        logger.info(
                            f"LeaderElection: became leader "
                            f"(instance_id={self._instance_id})"
                        )
                        self._is_leader = True
            except Exception as e:  # noqa: BLE001
                logger.error(f"LeaderElection loop error: {e}")

            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=BB_PROMOTER_LEADER_RENEW_S
                )
            except asyncio.TimeoutError:
                pass
