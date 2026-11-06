"""ELEVENLABS_DIALOGUE_POOL_SIZE caps the Text-to-Dialogue sockets a pool opens.

Every open TTD socket holds one ElevenLabs dialogue session, and the account's
sessions are limited, so the pool size is the lever that keeps a pod under
that limit: a pool opens sockets on demand (nothing is pre-warmed) up to
max(2n, n + 4) per worker and never past it. These tests pin that, including
under a burst where sockets are slow to connect — the case where every
waiting request opens its own socket.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import SocketUnavailable
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeSocket, _wait_for


class SlowConnect:
    """Connector whose sockets take ``delay`` seconds to open, like a real
    WS handshake, so a burst sees them still connecting."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.sockets: list[FakeSocket] = []

    def __call__(self, uri, additional_headers=None, open_timeout=None):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                await asyncio.sleep(outer.delay)
                socket = FakeSocket()
                outer.sockets.append(socket)
                return socket

            async def __aexit__(self, *args):
                return False

        return _Ctx()


@pytest.fixture
def connect(monkeypatch):
    slow = SlowConnect(delay=0.05)
    monkeypatch.setattr(elevenlabs_pool, "connect", slow)
    return slow


@pytest.mark.parametrize("size, cap", [(4, 8), (2, 6), (11, 22)])
async def test_pool_size_sets_the_socket_cap(monkeypatch, size, cap):
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", size)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        pool = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert pool is not None
        assert pool._max_size == cap, "max(2n, n + 4) per pool per worker"
        assert pool._conns == [], "nothing pre-warmed: sockets open on demand"
    finally:
        await provider.aclose()


async def test_burst_never_opens_past_the_cap(monkeypatch, connect):
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", 4)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        pool = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert pool is not None
        pool._acquire_timeout = 1.0
        # 8 sockets x 4 usable contexts (keepalive holds the 5th) = 32. A
        # burst of 32 at once, while sockets are still connecting.
        held = await asyncio.gather(*(pool.acquire() for _ in range(32)))
        assert len(pool._conns) == 8
        assert len(connect.sockets) == 8, "the burst stopped at the cap"
        assert all(c.inflight == 4 for c in pool._conns), "every slot used"

        # Full: the next request waits instead of opening a 9th socket...
        pool._acquire_timeout = 0.2
        with pytest.raises(SocketUnavailable):
            await pool.acquire()
        assert len(connect.sockets) == 8

        # ...and gets a slot as soon as one frees up.
        pool._acquire_timeout = 2.0
        pool._cooldown_until = 0.0
        pool._acquire_failures = 0
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.1)
        assert not waiter.done()
        held[0].inflight -= 1
        got = await asyncio.wait_for(waiter, timeout=1.0)
        assert got is held[0]
        assert len(connect.sockets) == 8
        for conn in held:
            conn.inflight -= 1
        got.inflight -= 1
    finally:
        await provider.aclose()


async def test_sockets_open_only_as_demand_needs(monkeypatch, connect):
    """One request at a time never needs more than one socket."""
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", 4)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        pool = provider._get_pool(VOICE, V3_MODEL, False, "hi", 8000)
        assert pool is not None
        for _ in range(5):
            conn = await pool.acquire()
            await _wait_for(lambda: conn.ready.is_set())
            conn.inflight -= 1
        assert len(connect.sockets) == 1
    finally:
        await provider.aclose()
