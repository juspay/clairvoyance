"""The v2 dialler's own Redis client (spec 2026-10-05 §4.9, design card rule 50).

redis-py 7.1's async client retries a command three times on ConnectionError /
TimeoutError, with 1-10 s jittered sleeps, and re-sends EVALs. Every v2 script is
idempotent by (ticket id, owner) and every caller treats None as "not done" (the sweep,
the reaper and the ledger heal), so a library retry only adds seconds of sleep inside the
dial path. ``claim`` and ``mark_dialling`` retry once themselves, with the same owner.
Single node only (decision D14): REDIS_HOST / REDIS_PORT, like RedisService.
"""

from __future__ import annotations

from typing import Optional

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.core.config.static import (
    BB_V2_REDIS_MAX_CONNECTIONS,
    BB_V2_REDIS_SOCKET_TIMEOUT_S,
    REDIS_HOST,
    REDIS_PORT,
)

_CONNECT_TIMEOUT_S = 2.0

_client: Optional[aioredis.Redis] = None


def _no_retry() -> Retry:
    return Retry(NoBackoff(), 0)


async def v2_redis() -> aioredis.Redis:
    """The process's client, built on first use."""
    global _client
    if _client is None:
        pool = aioredis.BlockingConnectionPool(
            host=REDIS_HOST,
            port=int(REDIS_PORT),
            decode_responses=True,
            max_connections=BB_V2_REDIS_MAX_CONNECTIONS,
            # how long a command waits for a free connection: as long as for a reply
            timeout=BB_V2_REDIS_SOCKET_TIMEOUT_S,
            socket_timeout=BB_V2_REDIS_SOCKET_TIMEOUT_S,
            socket_connect_timeout=_CONNECT_TIMEOUT_S,
            # Given a pool, redis-py takes the retry policy from the pool's connection
            # kwargs (Redis(retry=...) is then ignored): this is the one that counts.
            retry=_no_retry(),
        )
        _client = aioredis.Redis(connection_pool=pool)
    return _client


async def close_v2_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose(close_connection_pool=True)
        _client = None
