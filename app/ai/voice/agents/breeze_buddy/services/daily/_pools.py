"""Process-wide HTTP pool for the Daily REST API.

Starting a voice session makes three calls to ``api.daily.co``: ``create_room``
then ``get_token`` twice (user, bot). They used to run inside
``async with create_aiohttp_session()``, so the connector — and every socket it
owned — was torn down as the block exited. The next voice session opened a cold
connection and paid the whole handshake again.

That handshake is not cheap. ``api.daily.co`` negotiates **TLS 1.2**, a
two-round-trip handshake, and the endpoint answers ~226ms away:

===================================  ==========
TCP + TLS to api.daily.co (median)     676 ms
the three calls, fresh session        1332 ms
the three calls, warm connection       720 ms
**saved per voice session**          **612 ms**
===================================  ==========

Nothing here is a protocol tweak — TLS 1.2 is the server's choice and session
tickets are not reused by ``aiohttp``. The only way to stop paying for a
handshake is to stop performing one, so this module keeps a single
``ClientSession`` alive for the life of the pod. The first voice session after
boot pays 676ms; every session after it starts the room on an open connection.

Three constraints shape the implementation:

1. **A ``ClientSession`` belongs to one event loop.** Unlike ``httpx``,
   ``aiohttp`` captures the running loop at construction and raises if used from
   another one. So the loop is stored beside the session and identity-checked on
   every hand-out — otherwise the bot subprocess (its own loop) and the test
   suite (a loop per test) would both detonate on a session built elsewhere.

2. **A pooled connection dies if left idle.** ``api.daily.co`` advertises no
   ``Keep-Alive: timeout=``, so it was measured: a connection idle for 60s was
   still reused, one idle for 120s came back as a fresh 933ms handshake. Daily
   hangs up somewhere in between. ``BB_DAILY_REST_KEEPALIVE_SECS`` (45s) is set
   below the proven-safe 60s so we are always the side that closes first, the
   warmer below stops connections ever getting that idle, and
   ``call_with_retry`` covers the residual race — a deploy or load-balancer
   rotation closing a connection we already picked up.

3. **The zygote must never inherit this pool.** Live TLS sockets across a fork
   mean two processes writing into one TLS stream, which corrupts it for both.
   ``start_zygote()`` runs in ``run.py`` before uvicorn, and this pool cannot
   exist without a running loop, so the ordering already forbids it — but that
   is the reason to keep pool creation loop-bound rather than lazy-global.

Two callers share this session. ``daily.start_daily_session`` creates the room
and then mints its two meeting tokens *concurrently*, which is why the warmer
opens two connections: HTTP/1.1 cannot multiplex, so the second token request
needs a second socket or it pays a handshake. ``daily.recording`` reuses the
same connections for recording downloads.

Bots are unaffected: each lives for one call and gets its tokens handed to it
over the launch payload, so it never talks to this API at all.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional, TypeVar

import aiohttp

from app.core.config.static import (
    BB_DAILY_REST_CONNECT_TIMEOUT_SECS,
    BB_DAILY_REST_KEEPALIVE_SECS,
    BB_DAILY_REST_POOL_LIMIT,
    BB_DAILY_REST_TOTAL_TIMEOUT_SECS,
    BB_DAILY_REST_WARM_CONNECTIONS,
    BB_DAILY_REST_WARM_INTERVAL_SECS,
    BREEZE_BUDDY_DAILY_API_KEY,
    BREEZE_BUDDY_DAILY_API_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session

__all__ = [
    "call_with_retry",
    "close_daily_rest_pool",
    "get_daily_rest_session",
    "start_daily_rest_warmer",
    "stop_daily_rest_warmer",
    "warm_daily_rest_pool",
]

T = TypeVar("T")

# Monotonic stamp of the last real Daily call, so the warmer can tell an
# idle line from one voice sessions are already keeping open.
_last_real_call: float = 0.0

_session: Optional[aiohttp.ClientSession] = None
_session_loop: Optional[asyncio.AbstractEventLoop] = None


def get_daily_rest_session() -> aiohttp.ClientSession:
    """Return the pod's shared Daily REST session, building it on first use.

    Must be called from a running loop. The session is rebuilt if it was closed
    or belongs to a different loop than the caller's; both are abnormal in the
    API process and normal in tests.
    """
    global _session, _session_loop

    loop = asyncio.get_running_loop()
    if _session is not None and not _session.closed and _session_loop is loop:
        return _session

    if _session is not None and not _session.closed:
        _abandon_stale_session(_session, _session_loop)

    connector = aiohttp.TCPConnector(
        limit=BB_DAILY_REST_POOL_LIMIT,
        # Close idle connections before the peer does. Daily advertises no
        # keep-alive timeout, so we pick one we know is under it and make
        # ourselves the side that hangs up.
        keepalive_timeout=BB_DAILY_REST_KEEPALIVE_SECS,
    )
    _session = create_aiohttp_session(
        connector=connector,
        timeout=aiohttp.ClientTimeout(
            total=BB_DAILY_REST_TOTAL_TIMEOUT_SECS,
            connect=BB_DAILY_REST_CONNECT_TIMEOUT_SECS,
        ),
    )
    _session_loop = loop
    logger.info(
        f"Daily REST pool created (limit={BB_DAILY_REST_POOL_LIMIT}, "
        f"keepalive={BB_DAILY_REST_KEEPALIVE_SECS}s)"
    )
    return _session


def _abandon_stale_session(
    session: aiohttp.ClientSession, loop: Optional[asyncio.AbstractEventLoop]
) -> None:
    """Close a session bound to a loop we are no longer running on.

    ``close()`` is a coroutine that must run on the session's own loop, so it is
    scheduled there rather than awaited here. When that loop is already closed
    there is nothing to schedule on — but closing a loop closes its transports,
    so the sockets are already gone (measured: zero fd growth over five
    sessions stranded this way). What is left is aiohttp's bookkeeping and its
    "Unclosed client session" warning, hence the log rather than a cleanup: the
    API process runs one loop for its whole life and should never arrive here.
    """
    if loop is None or loop.is_closed():
        logger.warning("Daily REST pool: stale session on a closed loop; dropping it")
        return
    try:
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(session.close()))
    except RuntimeError as exc:
        logger.warning(f"Daily REST pool: could not close stale session: {exc!r}")


async def close_daily_rest_pool() -> None:
    """Close the shared session. Wired into FastAPI lifespan shutdown."""
    global _session, _session_loop

    session, _session, _session_loop = _session, None, None
    if session is None or session.closed:
        return
    try:
        await session.close()
    except Exception as exc:  # noqa: BLE001 - never block shutdown on a socket
        logger.warning(f"Daily REST pool close failed: {exc!r}")
        return
    logger.info("Daily REST pool closed")


def _is_stale_connection_error(exc: BaseException) -> bool:
    """True when ``exc`` proves the request never reached Daily.

    Replaying a call that Daily *did* process would create a second room —
    billed, never joined, and invisible to the caller, who only learns about
    one of them. So this admits only failures that happened before the server
    could act on the request. Exactly two qualify:

    ``ServerDisconnectedError``
        The peer closed a pooled connection without sending any response. This
        is the idle-timeout race the pool exists to absorb, and it was observed
        directly: a connection left idle past Daily's limit raises this on next
        use rather than reconnecting transparently.

    ``ClientConnectorError``
        No socket was ever obtained, so no byte was ever written.

    Their shared base ``ClientConnectionError`` is deliberately NOT used: it
    also covers ``ClientOSError``, a reset that can land *mid-response* — after
    Daily created the room. ``ClientResponseError`` means the server answered
    and rejected us, and ``asyncio.TimeoutError`` says nothing at all about
    whether the server acted; both must surface rather than replay.
    """
    return isinstance(
        exc, (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError)
    )


async def call_with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    what: str,
) -> T:
    """Run one Daily REST call, retrying once if a pooled connection was dead.

    ``operation`` is a zero-argument callable, not an awaitable, because a
    coroutine object cannot be awaited twice — the retry needs a fresh one.
    """
    global _last_real_call

    _last_real_call = time.monotonic()
    try:
        return await operation()
    except Exception as exc:
        if not _is_stale_connection_error(exc):
            raise
        logger.warning(
            f"Daily REST {what}: pooled connection was dead ({exc!r}); retrying once"
        )
    return await operation()


# ---------------------------------------------------------------------------
# keeping the pool warm
# ---------------------------------------------------------------------------
#
# Pooling alone only helps traffic dense enough that the next voice session
# arrives before the connection goes idle — and Daily hangs up between 60s and
# 120s of idleness (measured). Beta is not that traffic: calls arrive minutes
# apart, every one of them would find a dead pool, and the 612ms would come
# straight back. So a per-pod loop touches the connections every
# BB_DAILY_REST_WARM_INTERVAL_SECS, which keeps them below both that limit and
# our own keepalive. Measured end to end: a pod's first voice session costs
# 1760ms with no warmer and 437ms with one.
#
# Deliberately NOT a BackgroundTaskScheduler task. The scheduler derives a
# Redis lock key from the task name so exactly one pod runs each tick — right
# for reconcilers, wrong here: sockets are process-local, so every pod has to
# warm its own or the other pods stay cold forever.

_warm_task: Optional[asyncio.Task] = None


async def warm_daily_rest_pool() -> None:
    """Open the pod's connections to Daily and leave them idle in the pool.

    Best-effort by construction: Daily being unreachable must never fail a boot
    or kill the loop, it just means the next voice session pays the handshake
    it would have paid anyway.
    """
    session = get_daily_rest_session()
    url = f"{BREEZE_BUDDY_DAILY_API_URL.rstrip('/')}/"
    headers = {"Authorization": f"Bearer {BREEZE_BUDDY_DAILY_API_KEY}"}

    async def touch() -> None:
        async with session.get(url, headers=headers) as resp:
            await resp.read()

    # Concurrently, because that is what opens N distinct connections — N
    # sequential requests would reuse the first one and warm nothing else.
    results = await asyncio.gather(
        *(touch() for _ in range(BB_DAILY_REST_WARM_CONNECTIONS)),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, BaseException)]
    if failures:
        logger.warning(
            f"Daily REST pool warm-up: {len(failures)}/{len(results)} "
            f"connections failed ({failures[0]!r})"
        )


async def _keep_warm_loop() -> None:
    while True:
        try:
            # Real traffic resets Daily's idle timer just as well as a ping
            # does, so under load this loop should mostly do nothing.
            idle_for = time.monotonic() - _last_real_call
            if idle_for >= BB_DAILY_REST_WARM_INTERVAL_SECS:
                await warm_daily_rest_pool()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives any failure
            logger.warning(f"Daily REST pool warm-up failed: {exc!r}")
        await asyncio.sleep(BB_DAILY_REST_WARM_INTERVAL_SECS)


def start_daily_rest_warmer() -> None:
    """Start the per-pod keep-warm loop. Wired into FastAPI lifespan startup.

    A non-positive ``BB_DAILY_REST_KEEPALIVE_SECS`` is the kill switch for this
    whole feature — aiohttp then closes each connection on release, so warming
    would just generate traffic for connections nothing can reuse.
    """
    global _warm_task

    if BB_DAILY_REST_KEEPALIVE_SECS <= 0:
        logger.info("Daily REST pool disabled (keepalive=0); not starting warmer")
        return
    if _warm_task is not None and not _warm_task.done():
        return
    _warm_task = asyncio.create_task(_keep_warm_loop())
    logger.info(
        f"Daily REST pool warmer started "
        f"({BB_DAILY_REST_WARM_CONNECTIONS} connections every "
        f"{BB_DAILY_REST_WARM_INTERVAL_SECS}s)"
    )


async def stop_daily_rest_warmer() -> None:
    """Cancel the keep-warm loop. Must run before the session is closed."""
    global _warm_task

    task, _warm_task = _warm_task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
