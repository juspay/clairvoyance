"""Live ElevenLabs connection metrics (GET /stats/elevenlabs/live).

Pinned here: the gauges follow the real socket / sentence lifecycle and come
back to zero (drops, errors, close), peaks cover values that were held without
changes, a worker publishes at most once per interval and never while idle,
the pod view sums the live workers and drops dead ones, and the endpoint
serves it. Fake sockets / HTTP, no network.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys

import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import live as live_api
from app.core.config import settings
from app.providers import elevenlabs_live as live
from app.providers.elevenlabs import ElevenLabsProvider, pool_max_size
from app.providers.elevenlabs_live import LIVE, LiveStats, _Peak, windows
from app.providers.elevenlabs_pool import ElevenLabsStreamPool
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeConnect, _wait_for
from tests.test_flash_pipeline import RecordingHTTP


def _pool(connect: FakeConnect) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V3_MODEL,
        base_url=BASE,
        connect_fn=connect,
        min_size=1,
        max_size=1,
        idle_timeout=5.0,
    )


# ---------------------------------------------------------------------------
# Gauges follow the real lifecycle
# ---------------------------------------------------------------------------


async def test_socket_and_sentence_gauges_follow_the_lifecycle():
    open0 = LIVE.current(live.SOCKETS_OPEN)
    ws0 = LIVE.current(live.WS_IN_FLIGHT)
    dropped0 = LIVE.counters[live.SOCKETS_DROPPED]
    connect = FakeConnect()
    pool = _pool(connect)
    try:
        await pool.start()
        await pool._conns[0].ready.wait()
        assert LIVE.current(live.SOCKETS_OPEN) == open0 + 1
        assert pool.live_counts()["sockets"]["idle"] == 1

        chunks: list[bytes] = []

        async def speak():
            async for c in pool.stream({"text": "hi", "voice_id": VOICE}):
                chunks.append(c)

        task = asyncio.create_task(speak())
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        assert LIVE.current(live.WS_IN_FLIGHT) == ws0 + 1
        counts = pool.live_counts()
        assert counts["sockets"]["busy"] == 1
        assert counts["slots"] == {"total": 4, "used": 1}  # TTD: 4 usable

        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed({"context_id": ctx, "audio": base64.b64encode(b"ab").decode()})
        socket.feed({"context_id": ctx, "is_final_audio_for_turn": True})
        await asyncio.wait_for(task, 2)
        assert LIVE.current(live.WS_IN_FLIGHT) == ws0

        # The server drops the socket: it is no longer open, the drop is
        # counted, and the pool shows it waiting to reconnect.
        socket.feed(None)
        await _wait_for(lambda: LIVE.current(live.SOCKETS_OPEN) == open0)
        assert LIVE.counters[live.SOCKETS_DROPPED] == dropped0 + 1
        assert pool.live_counts()["sockets"]["reconnecting"] == 1
    finally:
        await pool.aclose()
    await _wait_for(lambda: LIVE.current(live.SOCKETS_OPEN) == open0)
    assert LIVE.counters[live.SOCKETS_DROPPED] == dropped0 + 1  # close != drop


async def test_closing_a_live_socket_is_not_a_drop():
    open0 = LIVE.current(live.SOCKETS_OPEN)
    dropped0 = LIVE.counters[live.SOCKETS_DROPPED]
    pool = _pool(FakeConnect())
    await pool.start()
    await pool._conns[0].ready.wait()
    assert LIVE.current(live.SOCKETS_OPEN) == open0 + 1
    await pool.aclose()  # our close (shutdown / refresh / reclaim), not a loss
    await _wait_for(lambda: LIVE.current(live.SOCKETS_OPEN) == open0)
    assert LIVE.counters[live.SOCKETS_DROPPED] == dropped0


async def test_a_failed_sentence_releases_its_slot():
    ws0 = LIVE.current(live.WS_IN_FLIGHT)
    connect = FakeConnect()
    pool = _pool(connect)
    try:
        await pool.start()
        await pool._conns[0].ready.wait()

        async def speak():
            async for _ in pool.stream({"text": "hi", "voice_id": VOICE}):
                pass

        task = asyncio.create_task(speak())
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        socket.feed(None)  # dies mid-sentence
        with pytest.raises(Exception):
            await asyncio.wait_for(task, 2)
        assert LIVE.current(live.WS_IN_FLIGHT) == ws0
    finally:
        await pool.aclose()


@pytest.mark.parametrize("fails", [False, True])
async def test_http_synth_is_counted_and_released(fails):
    requests0 = LIVE.counters[live.HTTP_REQUESTS]
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    http = RecordingHTTP(b"\x00\x01" * 800)
    seen: list[int] = []

    async def post(url, json=None, headers=None):
        seen.append(LIVE.current(live.HTTP_IN_FLIGHT))
        if fails:
            raise httpx.ConnectError("boom")
        return await RecordingHTTP.post(http, url, json=json, headers=headers)

    setattr(http, "post", post)
    setattr(provider, "_client", http)
    in_flight0 = LIVE.current(live.HTTP_IN_FLIGHT)
    try:
        call = provider.synth(
            text="hello",
            voice_id=VOICE,
            model="eleven_flash_v2_5",
            language="en",
            params={},
        )
        if fails:
            with pytest.raises(httpx.ConnectError):
                await call
        else:
            await call
    finally:
        await provider.aclose()
    assert seen == [in_flight0 + 1]  # in flight DURING the call
    assert LIVE.current(live.HTTP_IN_FLIGHT) == in_flight0
    assert LIVE.counters[live.HTTP_REQUESTS] == requests0 + 1


# ---------------------------------------------------------------------------
# Peaks
# ---------------------------------------------------------------------------


def test_peak_windows_cover_held_values():
    t0 = 1_000_000 * 60.0  # a minute boundary
    p = _Peak()
    # 0 -> 5 at minute 0, held, 5 -> 1 at minute 7 (the change records the
    # held 5 in minute 7 too).
    p.note(5, t0)
    p.note(5, t0 + 7 * 60)
    assert windows(p.buckets, 1, p.all, t0 + 8 * 60) == {
        "5m": 5,
        "15m": 5,
        "60m": 5,
        "since_start": 5,
    }
    assert windows(p.buckets, 1, p.all, t0 + 13 * 60)["5m"] == 1
    assert windows(p.buckets, 1, p.all, t0 + 13 * 60)["15m"] == 5
    # No changes at all for an hour: the current value is still the peak.
    assert windows({}, 3, 3, t0)["5m"] == 3


def test_add_records_the_value_held_before_a_drop(monkeypatch):
    now = [1_000_000 * 60.0]

    class _T:
        @staticmethod
        def time():
            return now[0]

        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(live, "time", _T)
    st = LiveStats()
    for _ in range(4):
        st.add(live.SOCKETS_OPEN, V3_MODEL, None, 1)
    now[0] += 10 * 60  # held at 4 for 10 minutes
    st.add(live.SOCKETS_OPEN, V3_MODEL, None, -3)
    now[0] += 60
    snap = st.snapshot()
    peak = snap["peaks"][live.SOCKETS_OPEN]
    buckets = {int(m): v for m, v in peak["buckets"].items()}
    assert windows(buckets, 1, peak["all"], now[0])["5m"] == 4
    assert snap["peaks"][f"{live.SOCKETS_OPEN}@default"]["all"] == 4


# ---------------------------------------------------------------------------
# Publishing: throttled, only after a change
# ---------------------------------------------------------------------------


async def test_publishes_at_most_once_per_interval_and_never_idle(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(live, "_PUBLISH_EVERY_SECS", 0.1)
    st = LiveStats()
    writes = []
    real = st.publish

    def counted():
        writes.append(1)
        real()

    monkeypatch.setattr(st, "publish", counted)
    st.start(lambda: [], directory=tmp_path)
    assert len(writes) == 1  # the startup snapshot
    for _ in range(200):
        st.add(live.WS_IN_FLIGHT, V3_MODEL, None, 1)
        st.add(live.WS_IN_FLIGHT, V3_MODEL, None, -1)
    await asyncio.sleep(0.25)
    assert len(writes) == 2  # 400 events -> one write
    await asyncio.sleep(0.3)
    assert len(writes) == 2  # idle -> nothing
    data = json.loads((tmp_path / f"worker-{os.getpid()}.json").read_text())
    assert data["totals"][live.WS_IN_FLIGHT] == 0
    st.stop()
    assert not tmp_path.exists()  # file and (now empty) directory removed


def test_not_started_never_touches_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "default_dir", lambda: tmp_path / "x")
    st = LiveStats()
    st.add(live.SOCKETS_OPEN, V3_MODEL, None, 1)
    st.publish()
    assert not (tmp_path / "x").exists()


# ---------------------------------------------------------------------------
# Pod view: sums live workers, drops dead ones
# ---------------------------------------------------------------------------


def _sibling(tmp_path, pid: int, sockets_open: int, account: str = "default") -> None:
    other = LiveStats()
    for _ in range(sockets_open):
        other.add(live.SOCKETS_OPEN, V3_MODEL, account, 1)
    snap = other.snapshot()
    snap["pid"] = pid
    (tmp_path / f"worker-{pid}.json").write_text(json.dumps(snap))


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_pod_sums_live_workers_and_removes_dead_ones(tmp_path):
    st = LiveStats()
    st.start(lambda: [], directory=tmp_path)
    st.add(live.SOCKETS_OPEN, V3_MODEL, None, 1)
    st.add(live.SOCKETS_OPEN, V3_MODEL, None, 1)
    alive = os.getppid()
    dead = _dead_pid()
    _sibling(tmp_path, alive, 3)
    _sibling(tmp_path, dead, 50)

    pod = st.pod()
    assert pod["workers_reporting"] == 2
    assert pod["sockets"]["open"] == 5  # 2 here + 3 on the live sibling
    assert pod["by_model"][V3_MODEL][live.SOCKETS_OPEN] == 5
    assert pod["by_account"]["default"][live.SOCKETS_OPEN] == 5
    assert not (tmp_path / f"worker-{dead}.json").exists()

    # Everything goes quiet: current is 0, the sampled pod peak stays.
    st.add(live.SOCKETS_OPEN, V3_MODEL, None, -2)
    _sibling(tmp_path, alive, 0)
    pod = st.pod()
    assert pod["sockets"]["open"] == 0
    assert pod["peaks"][live.SOCKETS_OPEN]["5m"] == 5
    assert pod["by_account"]["default"]["peak_sockets_open"]["5m"] == 5
    mine = next(w for w in pod["workers"] if w["pid"] == os.getpid())
    assert mine["peaks"][live.SOCKETS_OPEN]["since_start"] == 2  # exact, own
    st.stop()


def test_pod_peak_covers_a_value_held_across_minutes(monkeypatch, tmp_path):
    now = [1_000_000 * 60.0]

    class _T:
        @staticmethod
        def time():
            return now[0]

        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(live, "time", _T)
    st = LiveStats()
    st.start(lambda: [], directory=tmp_path)
    for _ in range(5):
        st.add(live.SOCKETS_OPEN, V3_MODEL, None, 1)
    st.pod()  # sampled at 5 in minute 0
    now[0] += 10 * 60  # held at 5 for 10 minutes, nobody published
    for _ in range(5):
        st.add(live.SOCKETS_OPEN, V3_MODEL, None, -1)
    assert st.pod()["peaks"][live.SOCKETS_OPEN]["5m"] == 5
    st.stop()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class _Registry:
    def __init__(self, provider) -> None:
        self.provider = provider

    def get(self, name: str):
        return self.provider if name == "elevenlabs" else None


async def _get(app: FastAPI) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.get("/stats/elevenlabs/live")


async def test_endpoint_serves_the_pod_view_with_limits(monkeypatch):
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    app = FastAPI()
    app.include_router(live_api.router)
    app.state.registry = _Registry(provider)
    try:
        r = await _get(app)
        assert r.status_code == 200
        body = r.json()
        for key in ("sockets", "in_flight", "slots", "peaks", "by_model", "counters"):
            assert key in body
        limits = body["limits"]
        assert limits["account_rotation"] is False
        assert limits["ttd_max_sockets_per_pool_per_worker"] == pool_max_size(
            settings.elevenlabs_dialogue_pool_size
        )
        assert pool_max_size(42) == 84 and pool_max_size(6) == 12

        monkeypatch.setattr(settings, "elevenlabs_live_metrics", False)
        assert (await _get(app)).status_code == 404
    finally:
        await provider.aclose()
