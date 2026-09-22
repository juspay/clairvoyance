"""Short-lived result cache for virtual try-on.

Covers exactly one failure: the shopper's page reloads mid-generation, so
their fetch dies while ours does not. Re-posting the same ``request_id``
finds the image here instead of paying for a second one.

Deliberately not a general cache — repeat views come from the shopper's
own browser, so a generated picture of a person lives on our servers only
as long as the failure it covers. Cache failures are never fatal: a miss
costs a regeneration, an unreachable Redis costs nothing. The request
claim is the exception: it fails closed.
"""

from typing import Optional

from app.core.config.dynamic import (
    TRY_ON_GENERATION_TIMEOUT_SECONDS,
    TRY_ON_RESULT_CACHE_TTL_SECONDS,
)
from app.core.logger import logger
from app.services.breeze_buddy.try_on.client import try_on_attempts
from app.services.redis.client import get_redis_service
from app.services.redis.locks import _RELEASE_LUA

_KEY_PREFIX = "try_on:result"
_CLAIM_PREFIX = "try_on:claim"

# How long a finished request_id stays claimed. Longer than the result
# cache on purpose: a replay after the image expired must not generate a
# second image on an id the ledger has already charged.
_DONE_TTL_SECONDS = 24 * 60 * 60


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


def _claim_key(session_id: str, request_id: str) -> str:
    return f"{_CLAIM_PREFIX}:{session_id}:{request_id}"


async def claim_try_on_request(session_id: str, request_id: str, token: str) -> str:
    """Reserve this request_id for one generation.

    Returns ``"claimed"``, or the state of the earlier claim: ``"running"``
    or ``"done"``. An unreachable Redis reads as ``"running"``: fails
    closed, like the IP limit.

    A running claim expires just after the longest a generation can take
    — every attempt the config allows, plus the garment download — so a
    crashed worker cannot block retries. Read from the same config the
    generator loops on: a claim that expired first would let a replay
    start a second generation on an id already being worked.
    ``token`` marks this claim as the caller's, for the release below.
    """
    key = _claim_key(session_id, request_id)
    running_ttl = (
        await try_on_attempts() * await TRY_ON_GENERATION_TIMEOUT_SECONDS() + 60
    )
    redis = await get_redis_service()
    if await redis.set(key, f"running:{token}", nx=True, ex=running_ttl):
        return "claimed"
    return "done" if await redis.get(key) == "done" else "running"


async def finish_try_on_request(session_id: str, request_id: str) -> None:
    """Mark the request_id as done once its image exists."""
    try:
        redis = await get_redis_service()
        await redis.set(
            _claim_key(session_id, request_id), "done", ex=_DONE_TTL_SECONDS
        )
    except Exception as exc:
        logger.warning(f"try-on claim finish failed request_id={request_id}: {exc}")


async def release_try_on_request(session_id: str, request_id: str, token: str) -> None:
    """Free the request_id after a failed generation, so a retry can use it.

    Only this caller's claim: after an overrun a retry may hold the key,
    and deleting it would let a third request generate alongside it.
    """
    try:
        redis = await get_redis_service()
        await redis.run_script(
            _RELEASE_LUA,
            keys=[_claim_key(session_id, request_id)],
            args=[f"running:{token}"],
        )
    except Exception as exc:
        logger.warning(f"try-on claim release failed request_id={request_id}: {exc}")
