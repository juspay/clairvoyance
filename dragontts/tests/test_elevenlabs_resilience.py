"""Text-to-Dialogue pool resilience: dropped sockets, half-open sockets,
retries under account rotation, and a seeded chaos run of rotation + socket
refresh + random drops under concurrency that checks every invariant at once
(caps never exceeded, no slot leaked, no empty clip, sentences survive).
Fake sockets — no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import random
import time

import pytest
from loguru import logger
from websockets.exceptions import ConnectionClosed

from app.providers import elevenlabs_live as live, elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_live import LIVE
from app.providers.elevenlabs_pool import ElevenLabsStreamPool, SocketUnavailable
from tests.test_elevenlabs_accounts import ENV, RecordingConnect, _budget
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeConnect, _wait_for


@pytest.fixture(autouse=True)
def _account_keys(monkeypatch):
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def logs():
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)


def _rotated(budget, connect, **kw) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        accounts=budget,
        idle_timeout=kw.pop("idle_timeout", 5.0),
        acquire_timeout=kw.pop("acquire_timeout", 1.0),
        **kw,
    )


async def _answer(socket, ctx: str, audio: bytes = b"\x01\x02" * 20) -> None:
    socket.feed({"context_id": ctx, "audio": base64.b64encode(audio).decode()})
    socket.feed({"context_id": ctx, "is_final_audio_for_turn": True})


# ---------------------------------------------------------------------------
# A socket reconnecting after a drop must not block fresh sockets (rotation)
# ---------------------------------------------------------------------------


async def test_a_reconnecting_socket_does_not_block_a_fresh_one():
    budget = _budget(rng=lambda: 0.0, a_max=8)  # india, 2 per worker
    connect = RecordingConnect()
    pool = _rotated(budget, connect, acquire_timeout=0.5)
    try:
        first = await pool.acquire()
        first.inflight -= 1
        connect.sockets[0].feed(None)  # server drops it: >= 1 s reconnect backoff
        await _wait_for(lambda: not first.ready.is_set())
        t0 = time.monotonic()
        conn = await pool.acquire()  # must not wait out the backoff
        assert time.monotonic() - t0 < 0.5
        assert conn is not first and budget.open["india"] == 2
        conn.inflight -= 1
    finally:
        await pool.aclose()


# ---------------------------------------------------------------------------
# Under rotation "no socket" is retried — the retry re-picks the account
# ---------------------------------------------------------------------------


async def test_rotation_retries_no_socket_on_a_re_picked_account(logs):
    picks = iter([0.9, 0.0])  # global (circuit open), then india
    budget = _budget(rng=lambda: next(picks))
    connect = RecordingConnect()
    pool = _rotated(budget, connect)
    pool._account_cooldown_until["global"] = time.monotonic() + 60
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        task = asyncio.create_task(
            provider._speak_v3(pool, {"text": "hi"}, voice_id=VOICE, model=V3_MODEL)
        )
        await _wait_for(
            lambda: connect.sockets
            and any(m.get("flush") for m in connect.sockets[0].sent)
        )
        socket = connect.sockets[0]
        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
        await _answer(socket, ctx)
        assert await asyncio.wait_for(task, 2.0) == b"\x01\x02" * 20
        assert pool._conns[0].account == "india"
        assert any("retry succeeded" in m for _, m in logs)
    finally:
        await pool.aclose()
        await provider.aclose()


async def test_single_key_still_does_not_retry_no_socket(logs):
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    pool = ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=FakeConnect(),
        min_size=0,
        max_size=0,
        acquire_timeout=0.1,
    )
    try:
        with pytest.raises(SocketUnavailable):
            await provider._speak_v3(
                pool, {"text": "hi"}, voice_id=VOICE, model=V3_MODEL
            )
        assert any("(not retried)" in m for _, m in logs)
    finally:
        await pool.aclose()
        await provider.aclose()


# ---------------------------------------------------------------------------
# A half-open socket (nothing received since the send) is skipped for a while
# ---------------------------------------------------------------------------


async def test_a_silent_socket_is_skipped_after_no_first_audio(monkeypatch, logs):
    monkeypatch.setattr(elevenlabs_pool, "_FIRST_AUDIO_TIMEOUT_SECS", 0.2)
    connect = FakeConnect()
    pool = ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        min_size=1,
        max_size=2,
    )
    await pool.start()
    try:
        dead = pool._conns[0]
        await dead.ready.wait()
        with pytest.raises(elevenlabs_pool.FirstAudioTimeout):
            async for _ in pool.stream({"text": "hi"}):
                pass
        assert dead.suspect_until > time.monotonic()
        assert any("possibly half-open" in m for _, m in logs)
        conn = await pool.acquire()  # a fresh socket, not the silent one
        assert conn is not dead
        conn.inflight -= 1
    finally:
        await pool.aclose()


async def test_a_socket_that_is_talking_is_not_suspected(monkeypatch):
    """No first audio for THIS sentence, but the socket is alive (another
    sentence's audio arrived on it) — the server is slow, not the link."""
    monkeypatch.setattr(elevenlabs_pool, "_FIRST_AUDIO_TIMEOUT_SECS", 0.3)
    connect = FakeConnect()
    pool = ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        min_size=1,
        max_size=2,
    )
    await pool.start()
    try:
        conn = pool._conns[0]
        await conn.ready.wait()

        async def other_traffic():
            await asyncio.sleep(0.1)
            connect.sockets[0].feed({"context_id": "someone-else", "audio": "AAAA"})

        bg = asyncio.create_task(other_traffic())
        with pytest.raises(elevenlabs_pool.FirstAudioTimeout):
            async for _ in pool.stream({"text": "hi"}):
                pass
        await bg
        assert conn.suspect_until <= time.monotonic()
        assert conn in pool._available()
    finally:
        await pool.aclose()


# ---------------------------------------------------------------------------
# Chaos: rotation + refresh + random drops under concurrency
# ---------------------------------------------------------------------------


class ChaosConnect:
    """Sockets that answer every flushed sentence after a short random delay,
    and with probability ``drop_p`` drop instead (mid-sentence, no audio)."""

    def __init__(self, rng: random.Random, drop_p: float) -> None:
        self.rng = rng
        self.drop_p = drop_p
        self.opened = 0
        self.drops = 0

    def __call__(self, uri: str, headers: dict):
        outer = self

        class Socket:
            def __init__(self) -> None:
                self._q: asyncio.Queue = asyncio.Queue()
                self.closed = False

            async def send(self, message: str) -> None:
                if self.closed:
                    from websockets.exceptions import ConnectionClosedError

                    raise ConnectionClosedError(None, None)
                m = json.loads(message)
                if m.get("flush"):
                    asyncio.get_running_loop().create_task(
                        self._respond(m["context_id"])
                    )

            async def _respond(self, ctx: str) -> None:
                await asyncio.sleep(outer.rng.uniform(0.005, 0.04))
                if self.closed:
                    return
                if outer.rng.random() < outer.drop_p:
                    outer.drops += 1
                    await self.close()
                    return
                audio = base64.b64encode(b"\x05\x06" * 30).decode()
                self._q.put_nowait(json.dumps({"context_id": ctx, "audio": audio}))
                self._q.put_nowait(
                    json.dumps({"context_id": ctx, "is_final_audio_for_turn": True})
                )

            async def close(self) -> None:
                if not self.closed:
                    self.closed = True
                    self._q.put_nowait(None)

            def __aiter__(self):
                return self

            async def __anext__(self) -> str:
                item = await self._q.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        class _Ctx:
            async def __aenter__(self):
                outer.opened += 1
                await asyncio.sleep(outer.rng.uniform(0.0, 0.02))
                return Socket()

            async def __aexit__(self, *args):
                return False

        return _Ctx()


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_chaos_rotation_refresh_and_drops(monkeypatch, seed):
    rng = random.Random(seed)
    open0 = LIVE.current(live.SOCKETS_OPEN)
    ws0 = LIVE.current(live.WS_IN_FLIGHT)
    budget = _budget(rng=rng.random, a_max=12, b_max=8)  # 3 + 2 per worker
    connect = ChaosConnect(rng, drop_p=0.08)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    pools = [
        _rotated(budget, connect, refresh_interval=0.15, acquire_timeout=3.0),
        _rotated(
            budget, connect, refresh_interval=0.15, acquire_timeout=3.0, language="en"
        ),
    ]
    over_cap: list[dict] = []
    stop = asyncio.Event()

    async def watch():
        while not stop.is_set():
            for name, n in budget.open.items():
                if n > budget.share[name]:
                    over_cap.append(dict(budget.open))
            await asyncio.sleep(0.002)

    watcher = asyncio.create_task(watch())
    results: list[bytes | Exception] = []
    gate = asyncio.Semaphore(24)

    async def one(i: int):
        async with gate:
            await asyncio.sleep(rng.uniform(0, 0.3))
            try:
                results.append(
                    await provider._speak_v3(
                        pools[i % 2],
                        {"text": f"s{i}", "voice_id": VOICE},
                        voice_id=VOICE,
                        model=V3_MODEL,
                    )
                )
            except Exception as e:  # recorded, judged below
                results.append(e)

    try:

        async def run_all() -> None:
            await asyncio.gather(*(one(i) for i in range(300)))

        await asyncio.wait_for(run_all(), 60)
    finally:
        stop.set()
        await watcher
        for p in pools:
            await p.aclose()
        await provider.aclose()

    ok = [r for r in results if isinstance(r, bytes)]
    failed = [r for r in results if not isinstance(r, bytes)]
    assert len(results) == 300
    assert over_cap == [], "an account went past its per-worker share"
    assert all(r == b"\x05\x06" * 30 for r in ok), "every clip complete, none empty"
    assert connect.drops > 10, "the chaos actually happened"
    # A sentence fails only when its attempt AND its retry both hit a drop,
    # and one drop kills every sentence in flight on that socket, so the count
    # tracks the drops: measured 2-14 of 300 across many runs (timing varies
    # run to run even with a fixed seed). The starvation bug this guards
    # against failed 136 of 300 — 30 (10 %) separates the two with margin.
    assert len(failed) <= 30, (len(failed), [type(e).__name__ for e in failed][:5])
    # ConnectionClosed: the retry's socket dropped between pick and send (the
    # pool re-raises it as-is; the router maps it to the same 502). Rare —
    # about 1 in 50 suite runs — but a legitimate failure under drops.
    assert all(
        isinstance(
            e, (elevenlabs_pool.ProviderError, SocketUnavailable, ConnectionClosed)
        )
        for e in failed
    ), [type(e).__name__ for e in failed]
    assert budget.open == {"india": 0, "global": 0}, "every slot returned"
    # The live metrics gauges survive drops, retries and refreshes: nothing
    # left counted as open or in flight once everything is closed.
    assert LIVE.current(live.WS_IN_FLIGHT) == ws0
    await _wait_for(lambda: LIVE.current(live.SOCKETS_OPEN) == open0)
