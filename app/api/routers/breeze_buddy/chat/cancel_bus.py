"""
Cross-pod cancel signalling for in-flight chat turns.

Problem this solves
-------------------
``send_chat_message_handler`` opens an SSE stream and holds the
per-session Redis lock until ``turn_end``. If the client aborts the
fetch (the widget Stop button), TCP close is only surfaced when the
server tries to write to the socket — and the agent is usually deep in
an LLM call or MCP tool call, not yielding. So the lock stays held
until its 180s TTL elapses, blocking any follow-up ``/message`` POST
on the same session with a 409 ("Another turn is already in flight").

How this works
--------------
Two cooperating pieces:

1. **Per-pod task registry** (``_active_tasks``): a process-local
   ``Dict[session_id, asyncio.Task]`` populated by ``register()`` /
   cleared by ``unregister()`` inside the stream generator. Only the
   pod that owns the running task can cancel it from inside its own
   event loop — that's the whole point of the local registry.

2. **Cross-pod fanout** via Redis pubsub on channel ``_CANCEL_CHANNEL``.
   ``cancel(session_id)`` publishes the session id. Every pod runs one
   long-lived subscriber (started in app lifespan) that looks up the
   session in its local registry and calls ``task.cancel()`` if found.
   Pods that don't own the task ignore the message.

Cost model
----------
- One persistent Redis subscriber connection per pod (idle blocking on
  ``get_message``; ~0 CPU when no cancels are firing).
- ``_active_tasks`` adds ~200 bytes per concurrent in-flight stream.
- Zero overhead on the hot path: the stream generator does NOT poll
  Redis between yields. The hot path runs unchanged.

Single-pod (local dev) works because pod A == pod B — the publish
fanout still reaches the local subscriber.

Failure modes
-------------
- Redis down at app start: subscriber start fails; cancel becomes a
  no-op (falls back to TTL). Logged; not raised — the app shouldn't
  fail to start because cancel is degraded.
- ``task.cancel()`` racing with natural turn-end: harmless — the task
  is already done, ``cancel()`` returns ``False``.
"""

from __future__ import annotations

import asyncio
from typing import Dict, Optional

from redis.exceptions import RedisError

from app.core.logger import logger
from app.services.redis.client import RedisService, get_redis_service

# Redis pubsub channel name. Single channel for all sessions — message
# payload is the session_id. One channel keeps the subscriber simple
# (no per-session subscribe/unsubscribe churn).
_CANCEL_CHANNEL = "breeze-buddy:chat:cancel"

# Process-local registry: session_id → the asyncio Task running the
# SSE stream for that session on THIS pod. Populated by
# ``register()`` inside the stream generator; cleared by
# ``unregister()`` in its finally block.
_active_tasks: Dict[str, asyncio.Task] = {}

# Handle to the subscriber task started by ``start_subscriber()`` so
# ``stop_subscriber()`` can cancel it on shutdown. ``None`` when the
# subscriber isn't running (Redis unconfigured or start failed).
_subscriber_task: Optional[asyncio.Task] = None


def register(session_id: str, task: asyncio.Task) -> None:
    """Mark ``task`` as the in-flight stream for ``session_id`` on this pod.

    Overwrites any prior registration for the same session — the Redis
    lock already enforces single-driver semantics, so a duplicate
    registration would indicate a programming error (lock check
    bypassed) rather than legitimate concurrent streams. We log and
    overwrite rather than raise so the request still gets served.
    """
    prior = _active_tasks.get(session_id)
    if prior is not None and not prior.done():
        logger.warning(
            f"cancel_bus: overwriting active task for session={session_id} "
            "— prior task wasn't unregistered (Redis lock invariant broken?)"
        )
    _active_tasks[session_id] = task


def unregister(session_id: str, task: asyncio.Task) -> None:
    """Remove ``task`` from the registry IFF it's still the registered one.

    Guarded by identity comparison so a stale ``finally`` from an older
    cancelled task doesn't evict the registration for a newer turn that
    raced past it.
    """
    current = _active_tasks.get(session_id)
    if current is task:
        _active_tasks.pop(session_id, None)


async def cancel(session_id: str) -> None:
    """Publish a cancel for ``session_id`` to every pod.

    Best-effort. Returns immediately after publish; the actual
    ``task.cancel()`` happens asynchronously on whichever pod owns the
    task. If Redis is down, this no-ops with a warning — the caller
    (HTTP handler) will still return 202; the lock will release on
    TTL.
    """
    try:
        redis: RedisService = await get_redis_service()
        client = await redis.get_client()
        # `client` is typed `Union[Redis, RedisCluster]`. Single-node Redis
        # exposes publish/pubsub directly; RedisCluster's pubsub semantics
        # differ (shard-scoped) and would need a separate code path. We run
        # single-node in dev + prod today; if we ever move to Cluster, this
        # whole module needs a cluster-aware pubsub rewrite anyway.
        await client.publish(_CANCEL_CHANNEL, session_id)  # type: ignore[union-attr]
        logger.debug(f"cancel_bus: published cancel for session={session_id}")
    except (RedisError, Exception) as exc:
        logger.warning(
            f"cancel_bus: publish failed for session={session_id}: {exc} "
            "— stream will only release on lock TTL"
        )


def _cancel_local(session_id: str) -> bool:
    """Cancel the locally-registered task for ``session_id`` if any.

    Returns ``True`` if a task was cancelled, ``False`` if the session
    isn't running on this pod (which is the common case for multi-pod
    deployments — only one pod ever owns a given session at a time).
    """
    task = _active_tasks.get(session_id)
    if task is None:
        return False
    if task.done():
        return False
    task.cancel()
    logger.info(f"cancel_bus: cancelled local task for session={session_id}")
    return True


async def _subscriber_loop() -> None:
    """Long-lived loop: subscribe to ``_CANCEL_CHANNEL`` and fan messages
    out to the local registry.

    Reconnects with exponential backoff on Redis errors — the
    subscriber must outlive transient network blips so cancel keeps
    working after a Redis restart. Backoff caps at 30s.
    """
    backoff = 1.0
    while True:
        try:
            redis: RedisService = await get_redis_service()
            client = await redis.get_client()
            # See `cancel()` above — single-node only path; Cluster's pubsub
            # is shard-scoped and would need a separate connection model.
            pubsub = client.pubsub()  # type: ignore[union-attr]
            try:
                await pubsub.subscribe(_CANCEL_CHANNEL)
                logger.info(f"cancel_bus: subscribed to '{_CANCEL_CHANNEL}'")
                backoff = 1.0
                # ``listen()`` yields messages forever; the only normal
                # exit is task cancellation on app shutdown.
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        # Skip 'subscribe' confirmation + heartbeats.
                        continue
                    data = message.get("data")
                    if isinstance(data, bytes):
                        session_id = data.decode("utf-8", errors="replace")
                    else:
                        session_id = str(data)
                    _cancel_local(session_id)
            finally:
                try:
                    await pubsub.unsubscribe(_CANCEL_CHANNEL)
                    await pubsub.aclose()
                except Exception:
                    pass
        except asyncio.CancelledError:
            logger.info("cancel_bus: subscriber cancelled (app shutdown)")
            raise
        except Exception as exc:
            # loguru: no ``exc_info`` kwarg — it would re-format the message
            # and crash on brace-bearing error text.
            logger.exception(
                f"cancel_bus: subscriber error ({exc!r}); "
                f"reconnecting in {backoff:.1f}s"
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def start_subscriber() -> None:
    """Start the pubsub subscriber as a background asyncio task.

    Idempotent — calling twice is a no-op. Safe to call from the app
    lifespan startup. If Redis isn't configured, this logs and returns
    without starting the loop (cancel falls back to TTL).
    """
    global _subscriber_task
    if _subscriber_task is not None and not _subscriber_task.done():
        return
    try:
        # Probe Redis once before spawning the loop — gives a clean
        # log line on misconfiguration instead of a tight reconnect
        # spin on first message.
        redis = await get_redis_service()
        await redis.ping()
    except Exception as exc:
        logger.warning(
            f"cancel_bus: Redis unreachable at startup ({exc!r}); "
            "subscriber not started — cancel will fall back to lock TTL"
        )
        return
    _subscriber_task = asyncio.create_task(
        _subscriber_loop(), name="breeze-buddy-cancel-bus-subscriber"
    )
    logger.info("cancel_bus: subscriber task started")


async def stop_subscriber() -> None:
    """Cancel the pubsub subscriber. Idempotent."""
    global _subscriber_task
    task = _subscriber_task
    _subscriber_task = None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


__all__ = [
    "cancel",
    "register",
    "start_subscriber",
    "stop_subscriber",
    "unregister",
]
