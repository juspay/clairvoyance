"""Tests for the pod-wide Daily REST connection pool.

``services/daily/_pools.py`` keeps one ``aiohttp.ClientSession`` alive for the
life of the process so voice sessions stop paying a fresh TCP + TLS handshake
to ``api.daily.co`` on every call start (measured 612ms). The invariants that
matter: one session per event loop, a session bound to a dead loop is never
handed out, shutdown closes it, and ``call_with_retry`` only replays a call
when the error proves Daily never saw it.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from app.ai.voice.agents.breeze_buddy.services.daily import _pools


@pytest.fixture(autouse=True)
async def _clean_pool():
    """Never let a test inherit or leak the module-global session or warmer."""
    await _pools.stop_daily_rest_warmer()
    await _pools.close_daily_rest_pool()
    yield
    await _pools.stop_daily_rest_warmer()
    await _pools.close_daily_rest_pool()


# ---------------------------------------------------------------------------
# session identity and lifecycle
# ---------------------------------------------------------------------------


async def test_same_loop_reuses_one_session():
    """The whole point: two callers share a connection, so only one handshake."""
    first = _pools.get_daily_rest_session()
    second = _pools.get_daily_rest_session()
    assert first is second
    assert not first.closed


async def test_session_is_configured_for_keepalive():
    """keepalive_timeout is what stops the connector discarding warm sockets."""
    session = _pools.get_daily_rest_session()
    connector = session.connector
    assert connector is not None
    assert connector._keepalive_timeout > 0


async def test_closed_session_is_rebuilt():
    """A session closed out from under us must not be handed out again."""
    first = _pools.get_daily_rest_session()
    await first.close()
    second = _pools.get_daily_rest_session()
    assert second is not first
    assert not second.closed


async def test_shutdown_closes_the_session():
    session = _pools.get_daily_rest_session()
    await _pools.close_daily_rest_pool()
    assert session.closed


async def test_shutdown_is_idempotent():
    """Lifespan shutdown can run after a failed startup; it must not raise."""
    _pools.get_daily_rest_session()
    await _pools.close_daily_rest_pool()
    await _pools.close_daily_rest_pool()


def test_session_from_a_dead_loop_is_never_handed_out():
    """aiohttp binds a session to its creating loop; reusing it across loops
    raises at request time, so a loop change must rebuild."""
    holder: dict = {}

    async def build():
        holder["session"] = _pools.get_daily_rest_session()

    asyncio.run(build())  # loop is closed on return
    stale = holder["session"]

    async def rebuild():
        fresh = _pools.get_daily_rest_session()
        assert fresh is not stale
        await _pools.close_daily_rest_pool()

    asyncio.run(rebuild())


# ---------------------------------------------------------------------------
# call_with_retry
# ---------------------------------------------------------------------------


async def test_retry_returns_the_value_and_calls_once_on_success():
    calls = []

    async def op():
        calls.append(1)
        return "room"

    assert await _pools.call_with_retry(op, what="create_room") == "room"
    assert len(calls) == 1


async def test_a_real_daily_error_is_not_replayed():
    """Replaying a call Daily actually processed would create a second room."""
    calls = []

    async def op():
        calls.append(1)
        raise ValueError("Daily rejected the room params")

    with pytest.raises(ValueError):
        await _pools.call_with_retry(op, what="create_room")
    assert len(calls) == 1, "a server-side rejection must never be retried"


async def test_stale_pooled_connection_is_retried_once():
    """The failure mode a per-call session could not have: the pooled socket
    was closed by the peer between voice sessions."""
    calls = []

    async def op():
        calls.append(1)
        if len(calls) == 1:
            raise aiohttp.ServerDisconnectedError("connection closed by peer")
        return "room"

    assert await _pools.call_with_retry(op, what="create_room") == "room"
    assert len(calls) == 2


async def test_retry_gives_up_after_one_replay():
    """Daily being genuinely down must surface, not spin."""
    calls = []

    async def op():
        calls.append(1)
        raise aiohttp.ServerDisconnectedError("still down")

    with pytest.raises(aiohttp.ServerDisconnectedError):
        await _pools.call_with_retry(op, what="create_room")
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# the warmer
# ---------------------------------------------------------------------------


async def test_warmer_opens_one_connection_per_configured_slot(monkeypatch):
    """Concurrently, not sequentially — sequential requests reuse the first
    connection and leave the rest of the pool cold."""
    inflight = 0
    peak = 0

    class _Resp:
        async def __aenter__(self):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.01)
            return self

        async def __aexit__(self, *exc):
            nonlocal inflight
            inflight -= 1

        async def read(self):
            return b""

    session = _pools.get_daily_rest_session()
    monkeypatch.setattr(session, "get", lambda *a, **k: _Resp())
    monkeypatch.setattr(_pools, "BB_DAILY_REST_WARM_CONNECTIONS", 2)

    await _pools.warm_daily_rest_pool()
    assert peak == 2, "warm requests must overlap or they warm one connection"


async def test_warmer_survives_daily_being_unreachable(caplog):
    """Daily down must not fail a boot — the next call just pays a handshake."""

    class _Boom:
        async def __aenter__(self):
            raise aiohttp.ClientConnectorError(None, OSError("unreachable"))  # type: ignore[arg-type]

        async def __aexit__(self, *exc):
            return False

    session = _pools.get_daily_rest_session()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(session, "get", lambda *a, **k: _Boom())
        await _pools.warm_daily_rest_pool()  # must not raise


async def test_warmer_is_disabled_by_the_keepalive_kill_switch(monkeypatch):
    """BB_DAILY_REST_KEEPALIVE_SECS=0 reverts to a handshake per request, so
    there is nothing to keep warm."""
    monkeypatch.setattr(_pools, "BB_DAILY_REST_KEEPALIVE_SECS", 0)
    _pools.start_daily_rest_warmer()
    assert _pools._warm_task is None


async def test_warmer_starts_once_and_stops_cleanly(monkeypatch):
    monkeypatch.setattr(_pools, "BB_DAILY_REST_WARM_INTERVAL_SECS", 3600)

    async def _noop():
        return None

    monkeypatch.setattr(_pools, "warm_daily_rest_pool", _noop)

    _pools.start_daily_rest_warmer()
    task = _pools._warm_task
    assert task is not None
    _pools.start_daily_rest_warmer()
    assert _pools._warm_task is task, "a second start must not spawn a second loop"

    await _pools.stop_daily_rest_warmer()
    assert task.cancelled() or task.done()
    assert _pools._warm_task is None


async def test_stopping_a_warmer_that_never_started_is_a_noop():
    await _pools.stop_daily_rest_warmer()


async def test_a_real_call_suppresses_the_next_warm_ping(monkeypatch):
    """Voice traffic resets Daily's idle timer, so pinging on top of it is
    pure waste — the loop should stay quiet while calls are flowing."""
    pings = []

    async def _count():
        pings.append(1)

    monkeypatch.setattr(_pools, "warm_daily_rest_pool", _count)
    monkeypatch.setattr(_pools, "BB_DAILY_REST_WARM_INTERVAL_SECS", 30)

    async def op():
        return "room"

    await _pools.call_with_retry(op, what="create_room")  # stamps _last_real_call

    task = asyncio.create_task(_pools._keep_warm_loop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pings == [], "a ping fired even though a real call just used the line"


async def test_an_idle_line_still_gets_warmed(monkeypatch):
    """The case that makes this feature work at beta's call spacing."""
    pings = []

    async def _count():
        pings.append(1)

    monkeypatch.setattr(_pools, "warm_daily_rest_pool", _count)
    monkeypatch.setattr(_pools, "BB_DAILY_REST_WARM_INTERVAL_SECS", 30)
    monkeypatch.setattr(_pools, "_last_real_call", 0.0)  # nothing has called

    task = asyncio.create_task(_pools._keep_warm_loop())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pings == [1], "an idle line was never warmed"


def test_only_pre_server_failures_are_replayable():
    """The predicate that stands between a stale socket and a duplicate room."""
    stale = (
        aiohttp.ServerDisconnectedError("peer closed"),
        aiohttp.ClientConnectorError(None, OSError("no route")),  # type: ignore[arg-type]
    )
    for exc in stale:
        assert _pools._is_stale_connection_error(
            exc
        ), f"{type(exc).__name__} must retry"

    # Each of these could mean Daily already created the room.
    served = (
        aiohttp.ClientResponseError(None, (), status=500),  # type: ignore[arg-type]
        asyncio.TimeoutError(),
        aiohttp.ClientOSError("reset mid-response"),
        ValueError("bad params"),
    )
    for exc in served:
        assert not _pools._is_stale_connection_error(
            exc
        ), f"{type(exc).__name__} must NOT be replayed — it may have been processed"
