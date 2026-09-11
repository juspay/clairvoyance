"""Short-lived result cache for virtual try-on.

Covers exactly one failure: the shopper's page reloads mid-generation, so
their fetch dies while ours does not. Re-posting the same ``request_id``
finds the image here instead of paying for a second one.

Deliberately not a general cache — repeat views come from the shopper's
own browser, so a generated picture of a person lives on our servers only
as long as the failure it covers. Cache failures are never fatal: a miss
costs a regeneration, an unreachable Redis costs nothing.
"""

from typing import Optional

from app.core.config.dynamic import TRY_ON_RESULT_CACHE_TTL_SECONDS
from app.core.logger import logger
from app.services.redis.client import get_redis_service

_KEY_PREFIX = "try_on:result"


def _key(session_id: str, request_id: str) -> str:
    """Scope by session as well as request, so one session's id can never
    read another's image even if a request_id is guessed or replayed."""
    return f"{_KEY_PREFIX}:{session_id}:{request_id}"


async def get_cached_try_on_result(session_id: str, request_id: str) -> Optional[str]:
    """Return the image for a request already generated, if it is still held."""
    try:
        redis = await get_redis_service()
        return await redis.get(_key(session_id, request_id))
    except Exception as exc:
        logger.warning(
            f"try-on result cache read failed request_id={request_id}: {exc}"
        )
        return None


async def cache_try_on_result(session_id: str, request_id: str, image: str) -> None:
    """Hold one generated image against its request id."""
    try:
        ttl = await TRY_ON_RESULT_CACHE_TTL_SECONDS()
        redis = await get_redis_service()
        await redis.setex(_key(session_id, request_id), image, ttl)
    except Exception as exc:
        logger.warning(
            f"try-on result cache write failed request_id={request_id}: {exc}"
        )
