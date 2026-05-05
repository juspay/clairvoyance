"""Tiny Redis fixed-window rate limiter.

Used by the public chat-demo endpoints (CHAT_MODE.md §13) to bound per-IP
abuse: anonymous visitors can mint a demo token and stream LLM responses,
so we need a cheap way to cap session-creates and message volume.

Why fixed-window (not sliding/token-bucket): we're not protecting an SLA,
we're stopping bots. The boundary-burst behaviour is acceptable — a
visitor that times their requests perfectly to straddle two windows can
get 2x the limit, which is still bounded.

Storage shape: ``<prefix>:<bucket>:<seconds_since_epoch_floored_to_window>``.
Window is computed deterministically from ``time.time()`` so two pods
share the same bucket without coordination. TTL is the window length plus
a small grace period so a key created at second ``T`` lives long enough
to be readable until ``T + window``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from app.core.logger import logger
from app.services.redis import get_redis_service, is_redis_configured

__all__ = ["RateLimitDecision", "check_rate_limit"]


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of a rate-limit check.

    ``allowed`` is the only field most callers need; ``count`` and
    ``retry_after_seconds`` are exposed for clients that want to surface
    "you have N requests left" or a 429 ``Retry-After`` header.
    """

    allowed: bool
    count: int
    limit: int
    retry_after_seconds: int


async def check_rate_limit(
    *,
    bucket: str,
    identifier: str,
    limit: int,
    window_seconds: int,
    prefix: str = "ratelimit",
) -> RateLimitDecision:
    """Increment the counter for ``(bucket, identifier)``; deny when over.

    Args:
        bucket: Logical bucket name (e.g., ``demo_session_create``). Lets
            multiple limits coexist in Redis without colliding.
        identifier: The thing we're limiting against — typically client
            IP, but works for user_id, hashed-fingerprint, anything.
        limit: Max events allowed inside ``window_seconds``.
        window_seconds: Window size; current bucket index is
            ``floor(now / window_seconds)``.
        prefix: Redis key prefix. Override only if you need to isolate
            tests from prod (the default is fine for everything else).

    Returns ``allowed=True`` (and increments) when count <= limit, else
    ``allowed=False``. Fails *open* — if Redis is unconfigured or errors,
    we let the request through and log a warning. Demo abuse is preferable
    to a Redis hiccup taking down the demo entirely.
    """
    if not is_redis_configured():
        logger.warning(
            f"rate_limit: Redis not configured; bucket={bucket!r} "
            f"id={identifier!r} fail-open"
        )
        return RateLimitDecision(
            allowed=True, count=0, limit=limit, retry_after_seconds=0
        )

    if limit <= 0:
        # A non-positive limit means "disabled" — pass through without
        # touching Redis at all so callers can flip a feature off cheaply.
        return RateLimitDecision(
            allowed=True, count=0, limit=limit, retry_after_seconds=0
        )

    now = int(time.time())
    window_index = now // window_seconds
    key = f"{prefix}:{bucket}:{identifier}:{window_index}"
    seconds_into_window = now - (window_index * window_seconds)
    retry_after = max(window_seconds - seconds_into_window, 1)

    try:
        redis = await get_redis_service()
        count: Optional[int] = await redis.incr(key)
        if count == 1:
            # New bucket — set TTL just past the window edge so the key
            # is readable for the full window before Redis evicts it.
            await redis.expire(key, window_seconds + 5)
    except Exception as exc:
        logger.warning(
            f"rate_limit: Redis error for bucket={bucket!r} id={identifier!r}: "
            f"{exc} (fail-open)"
        )
        return RateLimitDecision(
            allowed=True, count=0, limit=limit, retry_after_seconds=0
        )

    if count is None:
        return RateLimitDecision(
            allowed=True, count=0, limit=limit, retry_after_seconds=0
        )

    return RateLimitDecision(
        allowed=count <= limit,
        count=count,
        limit=limit,
        retry_after_seconds=retry_after,
    )
