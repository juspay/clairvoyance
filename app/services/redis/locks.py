"""Distributed RedisLock async context manager.

Per-key Redis lock with unique-token release semantics so concurrent
holders cannot release each other's locks. ``acquire`` uses ``SET NX
EX``; ``release`` runs a Lua script that compares the caller's token
before deleting the key.

Use as::

    async with RedisLock(key, ttl_seconds=60) as lock:
        ...                       # protected work

The context manager raises :class:`LockAcquireError` if the key is
already held — callers should map that to whatever contention signal
makes sense for their surface (HTTP 409, retry, abort, …). The lock
has no mid-block renewal: pick a TTL that covers the worst-case
work; if work runs longer the holder has lost the lock and should
bail.

Distinct from the set-and-forget pattern in
``app/core/background_tasks/scheduler.py`` which uses ``SET NX EX``
once and lets the TTL handle "release" implicitly. That pattern is
fine for cron-style tasks where exactly-one-per-interval is the
goal, but unsafe whenever holders need to coordinate explicit
release or guard against another caller stealing a stale lock —
which is what this module is for.
"""

import uuid
from typing import Optional

from app.core.logger import logger
from app.services.redis.client import RedisService, get_redis_service


class LockAcquireError(Exception):
    """Raised when a :class:`RedisLock` cannot be acquired (already held)."""

    def __init__(self, key: str):
        super().__init__(f"Lock '{key}' is already held")
        self.key = key


# Compare-and-DEL: only release if our token still owns the key. Without
# the token check, a holder whose TTL elapsed (and whose work was picked
# up by another pod) would delete the *other pod's* fresh lock on
# release — silently breaking the mutual exclusion guarantee.
_RELEASE_LUA = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then "
    "  return redis.call('DEL', KEYS[1]) "
    "else return 0 end"
)


class RedisLock:
    """Per-key distributed lock with token-scoped release.

    Not re-entrant: acquiring twice on the same instance without an
    intervening release raises ``RuntimeError`` to surface the bug
    rather than silently leaking the prior token.
    """

    def __init__(
        self,
        key: str,
        ttl_seconds: int = 60,
        *,
        redis_service: Optional[RedisService] = None,
    ):
        """
        Args:
            key: Redis key. Should be namespaced per resource (e.g.
                ``"<domain>:<resource_type>:<id>:lock"``) so unrelated
                locks can't collide on the same key.
            ttl_seconds: Auto-expiry. If the holder dies without
                releasing, the lock is freed when the TTL elapses.
                There is no mid-block renewal — pick a TTL that
                covers the worst-case work.
            redis_service: Optional explicit service — useful in tests.
                When ``None``, fetched lazily on first use via
                ``get_redis_service()``.
        """
        self.key = key
        self.ttl_seconds = ttl_seconds
        self._redis = redis_service
        self._token: Optional[str] = None

    @property
    def token(self) -> Optional[str]:
        """The unique token issued at acquire-time, or ``None`` if not held."""
        return self._token

    async def _get_redis(self) -> RedisService:
        if self._redis is None:
            self._redis = await get_redis_service()
        return self._redis

    async def acquire(self) -> str:
        """Try to acquire the lock. Returns the token on success.

        Raises:
            LockAcquireError: Key is already held by another caller.
            RuntimeError: This instance is already holding the lock —
                indicates a programming error (double-acquire).
        """
        if self._token is not None:
            raise RuntimeError(f"RedisLock for key '{self.key}' already acquired")

        token = uuid.uuid4().hex
        redis = await self._get_redis()
        ok = await redis.set(self.key, token, nx=True, ex=self.ttl_seconds)
        if not ok:
            raise LockAcquireError(self.key)

        self._token = token
        logger.debug(
            f"Acquired RedisLock key='{self.key}' ttl={self.ttl_seconds}s "
            f"token={token[:8]}..."
        )
        return token

    async def release(self) -> bool:
        """Release the lock iff our token still owns it. Idempotent.

        Returns:
            ``True`` if our token was holder and we released it;
            ``False`` if the lock had already been freed (TTL elapsed,
            or another caller acquired after a stale TTL). Either way
            the caller's handle is cleared.
        """
        if self._token is None:
            return False

        redis = await self._get_redis()
        result = await redis.run_script(
            _RELEASE_LUA, keys=[self.key], args=[self._token]
        )
        released = bool(result)
        if not released:
            logger.warning(
                f"RedisLock key='{self.key}' token={self._token[:8]}... "
                "was already lost before release (TTL elapsed or stolen)"
            )
        else:
            logger.debug(
                f"Released RedisLock key='{self.key}' token={self._token[:8]}..."
            )

        self._token = None
        return released

    async def __aenter__(self) -> "RedisLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.release()


__all__ = ["RedisLock", "LockAcquireError"]
