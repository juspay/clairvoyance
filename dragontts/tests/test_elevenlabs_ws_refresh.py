"""ELEVENLABS_TTD_WS_REFRESH: Text-to-Dialogue sockets are replaced after an
interval, smoothly — the replacement opens first and the old socket keeps
serving until it is ready, in-flight sentences finish on the old socket, the
socket caps are never exceeded, and with the flag off nothing changes.
Fake sockets, no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from loguru import logger

from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import ElevenLabsStreamPool
from tests.test_elevenlabs_accounts import RecordingConnect, _budget, _config
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeConnect, _wait_for

INTERVAL = 0.2  # the pool checks every INTERVAL / 4


def _pool(connect, **kw) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        min_size=kw.pop("min_size", 1),
        max_size=kw.pop("max_size", 4),
        idle_timeout=5.0,
        refresh_interval=kw.pop("refresh_interval", INTERVAL),
        **kw,
    )


@pytest.fixture
def logs():
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)


async def test_off_sockets_are_never_replaced():
    connect = FakeConnect()
    pool = _pool(connect, refresh_interval=None)
    await pool.start()
    try:
        await pool._conns[0].ready.wait()
        await asyncio.sleep(INTERVAL * 3)
        assert len(connect.sockets) == 1 and pool._refresh_task is None
        assert not connect.sockets[0].closed
    finally:
        await pool.aclose()


async def test_an_old_socket_is_replaced_with_the_same_config():
    connect = FakeConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        await _wait_for(lambda: connect.sockets[0].closed, timeout=3.0)
        assert len(connect.uris) == 2 and connect.uris[0] == connect.uris[1]
        assert len(pool._conns) == 1 and pool._conns[0] is not old
        assert pool._conns[0].ready.is_set(), "a live socket is always there"
    finally:
        await pool.aclose()


async def test_an_in_flight_sentence_finishes_on_the_old_socket():
    connect = FakeConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        old_socket = connect.sockets[0]
        got: list[bytes] = []

        async def speak():
            async for chunk in pool.stream({"text": "नमस्ते"}):
                got.append(chunk)

        task = asyncio.create_task(speak())
        await _wait_for(lambda: any(m.get("flush") for m in old_socket.sent))
        ctx = next(m["context_id"] for m in old_socket.sent if m.get("flush"))
        old_socket.feed({"context_id": ctx, "audio": base64.b64encode(b"ab").decode()})

        # The refresh happens mid-sentence: replacement up, old one retired...
        await _wait_for(lambda: old.retiring, timeout=3.0)
        await asyncio.sleep(INTERVAL)  # several refresh passes
        assert not old_socket.closed, "not closed while a sentence is on it"
        # ...new sentences go to the replacement, never the retired socket...
        assert old not in pool._available()
        taken = [await pool.acquire() for _ in range(3)]
        assert all(c is not old for c in taken)
        for c in taken:
            c.inflight -= 1
        # ...and the old sentence completes whole.
        old_socket.feed({"context_id": ctx, "audio": base64.b64encode(b"cd").decode()})
        old_socket.feed({"context_id": ctx, "is_final_audio_for_turn": True})
        await asyncio.wait_for(task, 2.0)
        assert got == [b"ab", b"cd"]
        await _wait_for(lambda: old_socket.closed, timeout=2.0)
        assert old not in pool._conns
    finally:
        await pool.aclose()


async def test_the_old_socket_serves_until_the_replacement_is_ready(monkeypatch):
    """Replacement slow to connect: the old one keeps taking sentences."""
    connect = FakeConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        gate = asyncio.Event()

        class _Slow:
            def __init__(self, uri, headers):
                self.inner = connect(uri, headers)

            async def __aenter__(self):
                await gate.wait()
                return await self.inner.__aenter__()

            async def __aexit__(self, *args):
                return await self.inner.__aexit__(*args)

        pool._connect_fn = _Slow
        await _wait_for(lambda: pool._refresh_pending is not None, timeout=3.0)
        await asyncio.sleep(INTERVAL)
        assert not old.retiring
        conn = await pool.acquire()
        assert conn is old, "still serving while the replacement connects"
        conn.inflight -= 1
        gate.set()
        await _wait_for(lambda: old.retiring or old not in pool._conns, timeout=3.0)
    finally:
        await pool.aclose()


async def test_a_replacement_that_cannot_connect_is_dropped(monkeypatch, logs):
    monkeypatch.setattr(elevenlabs_pool, "_REFRESH_CONNECT_GRACE_SECS", 0.2)
    connect = RecordingConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        connect.fail_hosts = ("wss://",)  # every new socket fails
        await _wait_for(
            lambda: any("didn't connect" in m for _, m in logs), timeout=3.0
        )
        assert old in pool._conns and not old.retiring and old.ready.is_set()
        assert not connect.sockets[0].closed
    finally:
        await pool.aclose()


async def test_at_the_cap_the_refresh_waits_and_the_old_socket_keeps_serving():
    connect = FakeConnect()
    pool = _pool(connect, max_size=1)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        await asyncio.sleep(INTERVAL * 3)  # long past due
        assert pool._conns == [old] and not old.retiring
        assert len(connect.sockets) == 1 and not connect.sockets[0].closed
        conn = await pool.acquire()
        assert conn is old, "still serving"
        conn.inflight -= 1
        pool._max_size = 2  # room appears: the refresh goes ahead
        await _wait_for(lambda: connect.sockets[0].closed, timeout=3.0)
        assert len(pool._conns) == 1 and pool._conns[0] is not old
    finally:
        await pool.aclose()


async def test_one_socket_at_a_time_per_pool():
    connect = FakeConnect()
    pool = _pool(connect, min_size=3, max_size=8)
    await pool.start()
    try:
        await _wait_for(lambda: all(c.ready.is_set() for c in pool._conns))
        worst = 0

        async def sample():
            nonlocal worst
            while True:
                busy = sum(1 for c in pool._conns if c.retiring)
                busy += pool._refresh_pending is not None
                worst = max(worst, busy)
                await asyncio.sleep(0.005)

        sampler = asyncio.create_task(sample())
        await _wait_for(
            lambda: sum(s.closed for s in connect.sockets) >= 3, timeout=5.0
        )
        sampler.cancel()
        assert worst == 1
        # 3 live sockets, plus at most one replacement still connecting.
        assert 3 <= len(pool._conns) <= 4
    finally:
        await pool.aclose()


async def test_a_draining_socket_holds_off_the_next_refresh():
    connect = FakeConnect()
    pool = _pool(connect, min_size=2, max_size=8)
    await pool.start()
    try:
        await _wait_for(lambda: all(c.ready.is_set() for c in pool._conns))
        for c in list(pool._conns):
            c.inflight += 1  # long sentences: nothing drains
        await _wait_for(lambda: any(c.retiring for c in pool._conns), timeout=3.0)
        await asyncio.sleep(INTERVAL * 3)  # both originals are long past due
        assert sum(c.retiring for c in pool._conns) == 1
        assert pool._refresh_pending is None
        for c in pool._conns:
            c.inflight = 0
    finally:
        await pool.aclose()


async def test_a_stuck_sentence_is_failed_over_after_the_drain_ceiling(
    monkeypatch, logs
):
    """Past the ceiling the socket closes; its sentence errors (so the
    one-shot retry speaks it elsewhere) instead of hanging to a cut clip."""
    monkeypatch.setattr(elevenlabs_pool, "_REFRESH_MAX_DRAIN_SECS", 0.2)
    connect = FakeConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()

        async def speak():
            async for _ in pool.stream({"text": "नमस्ते"}):
                pass

        task = asyncio.create_task(speak())
        await _wait_for(lambda: any(m.get("flush") for m in connect.sockets[0].sent))
        with pytest.raises(elevenlabs_pool.ProviderError, match="closed by refresh"):
            await asyncio.wait_for(task, 3.0)
        assert connect.sockets[0].closed
        assert any("still on it" in m for level, m in logs if level == "WARNING")
    finally:
        await pool.aclose()


async def test_a_stuck_sentence_is_closed_after_the_drain_ceiling(monkeypatch, logs):
    monkeypatch.setattr(elevenlabs_pool, "_REFRESH_MAX_DRAIN_SECS", 0.2)
    connect = FakeConnect()
    pool = _pool(connect)
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        old.inflight += 1  # a context that never releases its slot
        await _wait_for(lambda: connect.sockets[0].closed, timeout=3.0)
        assert any("still on it" in m for level, m in logs if level == "WARNING")
    finally:
        await pool.aclose()


async def test_rotation_replaces_on_the_same_account_within_its_budget():
    budget = _budget(rng=lambda: 0.9)  # global, 2 per worker
    connect = RecordingConnect()
    pool = ElevenLabsStreamPool(
        api_key="",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        accounts=budget,
        idle_timeout=5.0,
        refresh_interval=INTERVAL,
    )
    try:
        conn = await pool.acquire()
        conn.inflight -= 1
        peak = 0

        async def sample():
            nonlocal peak
            while True:
                peak = max(peak, budget.open["global"])
                await asyncio.sleep(0.005)

        sampler = asyncio.create_task(sample())
        await _wait_for(lambda: connect.sockets[0].closed, timeout=3.0)
        sampler.cancel()
        assert connect.calls[0] == connect.calls[1], "same host + key"
        assert pool._conns[0].account == "global"
        assert peak <= budget.share["global"] and budget.open["global"] == 1
    finally:
        await pool.aclose()


async def test_rotation_at_a_full_budget_waits_for_room():
    budget = _budget(rng=lambda: 0.9, b_max=4)  # global, 1 per worker
    connect = RecordingConnect()
    pool = ElevenLabsStreamPool(
        api_key="",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        accounts=budget,
        idle_timeout=5.0,
        refresh_interval=INTERVAL,
    )
    try:
        conn = await pool.acquire()
        conn.inflight -= 1
        await asyncio.sleep(INTERVAL * 3)
        assert len(connect.calls) == 1 and not connect.sockets[0].closed
        assert budget.open["global"] == 1 and not conn.retiring
        budget.share["global"] = 2  # room appears
        await _wait_for(lambda: connect.sockets[0].closed, timeout=3.0)
        assert budget.open["global"] == 1 and connect.calls[0] == connect.calls[1]
    finally:
        await pool.aclose()


@pytest.mark.parametrize("rotate", [False, True])
async def test_provider_wires_refresh_to_dialogue_pools_only(monkeypatch, rotate):
    monkeypatch.setattr(settings, "elevenlabs_ttd_ws_refresh", True)
    monkeypatch.setattr(settings, "elevenlabs_ttd_ws_refresh_interval", 30.0)
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", rotate)
    monkeypatch.setattr(settings, "elevenlabs_accounts_config", json.dumps(_config()))
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        ttd = provider._get_pool(VOICE, "eleven_v4_turbo", False, "hi", 8000)
        classic = provider._get_pool(VOICE, "eleven_flash_v2_5", language="en")
        assert ttd is not None and ttd._refresh_interval == 30.0
        assert classic is not None and classic._refresh_interval is None
    finally:
        await provider.aclose()


async def test_provider_refresh_off_by_default(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", False)
    assert settings.elevenlabs_ttd_ws_refresh is False
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        ttd = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert ttd is not None and ttd._refresh_interval is None
    finally:
        await provider.aclose()


def _rotated(budget, connect, **kw) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        accounts=budget,
        idle_timeout=5.0,
        refresh_interval=kw.pop("refresh_interval", INTERVAL),
        **kw,
    )


async def test_a_full_account_does_not_hold_up_another_accounts_refresh():
    pick = {"r": 0.0}
    budget = _budget(rng=lambda: pick["r"], a_max=4, b_max=8)  # india 1, global 2
    connect = RecordingConnect()
    pool = _rotated(budget, connect, refresh_interval=None)
    try:
        india = await pool.acquire()  # oldest socket; india's budget is full
        india.inflight += 1  # busy, so it can't be swapped either
        await asyncio.sleep(0.02)
        pick["r"] = 0.9
        glob = await pool.acquire()
        glob.inflight -= 1
        pool._refresh_interval = INTERVAL
        pool._refresh_task = asyncio.create_task(pool._refresh_loop())
        global_socket = connect.sockets[1]
        await _wait_for(lambda: global_socket.closed, timeout=3.0)
        assert india in pool._conns and not connect.sockets[0].closed
        assert budget.open == {"india": 1, "global": 1}
        india.inflight -= 1
    finally:
        await pool.aclose()


async def test_at_the_cap_an_idle_socket_is_swapped_when_others_have_room():
    connect = FakeConnect()
    pool = _pool(connect, min_size=2, max_size=2)
    await pool.start()
    try:
        await _wait_for(lambda: all(c.ready.is_set() for c in pool._conns))
        await _wait_for(
            lambda: sum(s.closed for s in connect.sockets) >= 1, timeout=3.0
        )
        assert len(pool._conns) <= 2, "never past max_size"
        assert any(c.ready.is_set() for c in pool._conns), "a live socket remains"
        conn = await pool.acquire()  # capacity is there / reopens on demand
        conn.inflight -= 1
        assert len(pool._conns) <= 2
    finally:
        await pool.aclose()


async def test_refresh_takes_an_idle_slot_from_another_pool_when_the_budget_is_full():
    budget = _budget(rng=lambda: 0.9, b_max=8)  # global, 2 per worker
    connect = RecordingConnect()
    other = _rotated(budget, connect, refresh_interval=None, language="en")
    pool = _rotated(budget, connect, refresh_interval=None)
    try:
        idle = await other.acquire()
        idle.inflight -= 1
        idle.last_used = time.monotonic() - 60
        busy = await pool.acquire()  # stays busy: can't be swapped
        assert budget.open["global"] == 2
        pool._refresh_interval = INTERVAL
        pool._refresh_task = asyncio.create_task(pool._refresh_loop())
        await _wait_for(lambda: busy.retiring, timeout=3.0)
        assert other._conns == [], "the idle slot moved to the refresh"
        assert budget.open["global"] == 2, "never past the budget"
        busy.inflight -= 1
        await _wait_for(lambda: busy not in pool._conns, timeout=3.0)
        assert budget.open["global"] == 1
    finally:
        await pool.aclose()
        await other.aclose()


async def test_a_dropped_replacement_returns_its_account_slot(monkeypatch):
    monkeypatch.setattr(elevenlabs_pool, "_REFRESH_CONNECT_GRACE_SECS", 0.2)
    budget = _budget(rng=lambda: 0.9)  # global, 2 per worker
    connect = RecordingConnect()
    pool = _rotated(budget, connect)
    try:
        conn = await pool.acquire()
        conn.inflight -= 1
        connect.fail_hosts = ("wss://",)
        await _wait_for(lambda: pool._refresh_pending is not None, timeout=3.0)
        assert budget.open["global"] == 2
        await _wait_for(lambda: pool._refresh_pending is None, timeout=3.0)
        await asyncio.sleep(0.02)
        assert budget.open["global"] in (1, 2)  # 2 only if the next try began
        assert conn in pool._conns and not conn.retiring
    finally:
        await pool.aclose()
    assert budget.open["global"] == 0, "every slot returned"


async def test_a_retired_socket_does_not_reconnect():
    connect = FakeConnect()
    pool = _pool(connect, refresh_interval=None)
    await pool.start()
    try:
        conn = pool._conns[0]
        await conn.ready.wait()
        conn.retiring = True
        connect.sockets[0].feed(None)  # server drops it
        await asyncio.sleep(1.5)  # past the first reconnect backoff
        assert len(connect.sockets) == 1
    finally:
        await pool.aclose()


async def test_provider_clamps_a_too_short_interval(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_account_rotate", False)
    monkeypatch.setattr(settings, "elevenlabs_ttd_ws_refresh", True)
    monkeypatch.setattr(settings, "elevenlabs_ttd_ws_refresh_interval", 1.0)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        ttd = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert ttd is not None and ttd._refresh_interval == 5.0
    finally:
        await provider.aclose()


async def test_a_failed_replacement_waits_a_full_interval_before_the_next(
    monkeypatch,
):
    monkeypatch.setattr(elevenlabs_pool, "_REFRESH_CONNECT_GRACE_SECS", 0.1)
    connect = RecordingConnect()
    pool = _pool(connect, refresh_interval=0.6)  # checks every 0.15 s
    await pool.start()
    try:
        old = pool._conns[0]
        await old.ready.wait()
        connect.fail_hosts = ("wss://",)  # every replacement fails to connect
        await _wait_for(lambda: len(connect.calls) >= 2, timeout=3.0)
        first_try = time.monotonic()
        await asyncio.sleep(0.45)  # several checks, well inside one interval
        assert len(connect.calls) == 2, "no new replacement before the interval"
        await _wait_for(lambda: len(connect.calls) >= 3, timeout=3.0)
        assert time.monotonic() - first_try >= 0.6
        assert old in pool._conns and not old.retiring
    finally:
        await pool.aclose()
