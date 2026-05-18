"""
Channel-capacity semaphore — one Redis LIST per outbound number.

Each list ``bb:channel:{outbound_number_id}`` holds M opaque tokens, where
M = ``outbound_number.maximum_channels``. Workers ``BLPOP`` a token before
dialing; call-end webhooks ``LPUSH`` a token back. Token identity is
irrelevant — only the count matters.

This replaces the DB-counter ``increment_outbound_number_channels`` on the
hot path. The DB counter is retained as the eventually-consistent view used
by ``reconcile_channel_tokens`` to detect leaks.
"""

from __future__ import annotations

import secrets
from typing import Any, Optional, cast

from app.ai.voice.agents.breeze_buddy.dispatch.keys import channel_key
from app.core.config.static import BB_CHANNEL_BLPOP_TIMEOUT_S
from app.core.logger import logger
from app.services.redis import get_redis_service


def _new_token() -> str:
    """Tokens are opaque random strings — only the count matters."""
    return secrets.token_hex(8)


async def init_channel_semaphore(
    outbound_number_id: str, maximum_channels: int
) -> bool:
    """
    Initialise (or reset) the channel semaphore for an outbound number.

    Idempotent: ``DEL`` followed by ``RPUSH`` ensures exactly M tokens. Called
    by the reconciler when a channel LIST is missing (cold start, Redis loss).
    Direct invocations from ops paths should hold an external lock (e.g.,
    ``BackgroundTaskScheduler``'s per-task SET NX) to avoid concurrent reinit
    on the same key racing each other.
    """
    if maximum_channels <= 0:
        return False
    key = channel_key(outbound_number_id)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        async with client.pipeline(transaction=True) as pipe:
            pipe.delete(key)
            pipe.rpush(key, *[_new_token() for _ in range(maximum_channels)])
            await pipe.execute()
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"init_channel_semaphore failed for {outbound_number_id}: {e}")
        return False


async def topup_channel_tokens(outbound_number_id: str, count: int) -> int:
    """Add ``count`` tokens to an existing semaphore. No-op if count <= 0."""
    if count <= 0:
        return 0
    key = channel_key(outbound_number_id)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.rpush(key, *[_new_token() for _ in range(count)])
        return count
    except Exception as e:  # noqa: BLE001
        logger.error(f"topup_channel_tokens failed for {outbound_number_id}: {e}")
        return 0


async def trim_channel_tokens(outbound_number_id: str, count: int) -> int:
    """
    Remove up to ``count`` tokens from an existing semaphore (LPOP loop).
    Used by the reconciler when ``LLEN`` exceeds the expected free count.
    """
    if count <= 0:
        return 0
    key = channel_key(outbound_number_id)
    removed = 0
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        for _ in range(count):
            popped = await client.lpop(key)
            if popped is None:
                break
            removed += 1
        return removed
    except Exception as e:  # noqa: BLE001
        logger.error(f"trim_channel_tokens failed for {outbound_number_id}: {e}")
        return removed


async def acquire_channel_token(
    outbound_number_id: str, timeout_s: Optional[int] = None
) -> Optional[str]:
    """
    Block-pop a token. Returns the token string on success, None on timeout.

    Workers hold the token across ``provider.make_call``; on success the
    token is held until the call-end webhook calls ``release_channel_token``.
    On failure (exception, rate-limit, etc.) the worker must release manually.
    """
    key = channel_key(outbound_number_id)
    t = BB_CHANNEL_BLPOP_TIMEOUT_S if timeout_s is None else timeout_s
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        popped = await client.blpop(key, timeout=t)
        if popped is None:
            return None
        # redis-py returns (key, value) tuple from BLPOP
        _, token = popped
        return token
    except Exception as e:  # noqa: BLE001
        logger.error(f"acquire_channel_token failed for {outbound_number_id}: {e}")
        return None


async def release_channel_token(
    outbound_number_id: str, token: Optional[str] = None
) -> bool:
    """
    LPUSH a token back into the semaphore. If ``token`` is None, a fresh
    one is generated — safe because tokens are interchangeable; the only
    invariant is the count.

    Idempotency: ``reconcile_channel_tokens`` trims any over-count caused
    by duplicate webhook firings, so calling this from a duplicate webhook
    is self-correcting within one reconciler tick.
    """
    key = channel_key(outbound_number_id)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.lpush(key, token if token is not None else _new_token())
        return True
    except Exception as e:  # noqa: BLE001
        logger.error(f"release_channel_token failed for {outbound_number_id}: {e}")
        return False


async def channel_tokens_available(outbound_number_id: str) -> int:
    """LLEN — used by metrics and the reconciler."""
    key = channel_key(outbound_number_id)
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        return int(await client.llen(key))
    except Exception as e:  # noqa: BLE001
        logger.error(f"channel_tokens_available failed for {outbound_number_id}: {e}")
        return 0


async def channel_exists(outbound_number_id: str) -> bool:
    """EXISTS — distinguishes "uninitialised" from "saturated" (LLEN==0)."""
    key = channel_key(outbound_number_id)
    try:
        redis = await get_redis_service()
        return await redis.exists(key)
    except Exception as e:  # noqa: BLE001
        logger.error(f"channel_exists failed for {outbound_number_id}: {e}")
        return False
