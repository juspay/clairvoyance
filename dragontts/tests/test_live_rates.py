"""Live request / cache / ElevenLabs rates on GET /stats/elevenlabs/live.

Pinned here: rates count per minute and per second (a worker's own peaks
exact), the pod's per-minute counts are the workers' counts summed exactly,
sentences sent to ElevenLabs (socket or HTTP) count as requests and as one
in-flight gauge, an incoming /tts/stream request is in flight until its last
byte, cache hits / misses are counted through the real cache (write-behind on
and off), the hit rate follows, and a metrics failure never fails serving.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from app.cache.service import CacheService
from app.core.config import settings
from app.main import InflightTrackingMiddleware
from app.providers import elevenlabs_live as live
from app.providers.base import AudioResult, BaseTTSProvider
from app.providers.elevenlabs_live import LIVE, LiveStats, aggregate
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore

T0 = 1_000_000 * 60.0  # a minute boundary
MODEL = "eleven_v3_conversational"


@pytest.fixture
def clock(monkeypatch):
    now = [T0]

    class _T:
        @staticmethod
        def time():
            return now[0]

        @staticmethod
        def monotonic():
            return now[0]

    monkeypatch.setattr(live, "time", _T)
    return now


def _rate(snap: dict, r: str) -> dict:
    return aggregate([snap], live.time.time())["rates"][r]


# ---------------------------------------------------------------------------
# Rates
# ---------------------------------------------------------------------------


def test_rates_count_per_minute_and_per_second(clock):
    st = LiveStats()
    for _ in range(7):  # 7 in one second
        st.event(live.REQUESTS)
    clock[0] += 1
    for _ in range(3):
        st.event(live.REQUESTS)
    clock[0] += 60  # next minute
    st.event(live.REQUESTS, 2)

    r = _rate(st.snapshot(), live.REQUESTS)
    assert r["total"] == 12
    assert r["this_minute"] == 2
    assert r["last_minute"] == 10
    assert r["per_minute"]["5m"] == 12
    assert r["peak_per_minute"]["5m"] == 10
    assert r["peak_per_minute"]["since_start"] == 10
    assert r["peak_per_second"]["since_start"] == 7  # exact for one worker

    clock[0] += 20 * 60  # 20 quiet minutes
    r = _rate(st.snapshot(), live.REQUESTS)
    assert r["this_minute"] == 0 and r["per_minute"]["15m"] == 0
    assert r["peak_per_minute"]["15m"] == 0
    assert r["peak_per_minute"]["60m"] == 10


def test_the_current_second_counts_toward_the_peak(clock):
    st = LiveStats()
    for _ in range(5):
        st.event(live.CACHE_HITS)
    snap = st.snapshot()  # still inside that second
    assert snap["rates"][live.CACHE_HITS]["second_peak"]["all"] == 5


def test_pod_per_minute_counts_are_the_workers_summed_exactly(clock, tmp_path):
    a, b = LiveStats(), LiveStats()
    for _ in range(4):
        a.event(live.ELEVENLABS_REQUESTS)
    clock[0] += 0.5
    for _ in range(6):
        b.event(live.ELEVENLABS_REQUESTS)
    snap_b = b.snapshot()
    snap_b["pid"] = os.getppid()  # a live sibling
    a.start(lambda: [], directory=tmp_path)
    (tmp_path / f"worker-{os.getppid()}.json").write_text(json.dumps(snap_b))

    pod = a.pod()
    r = pod["rates"][live.ELEVENLABS_REQUESTS]
    assert r["total"] == 10 and r["this_minute"] == 10
    assert r["peak_per_minute"]["5m"] == 10
    assert r["peak_per_second"]["5m"] == 10  # same second on both workers

    # Seconds later the pod's 10/s is kept by the sample (each worker alone
    # only ever saw 4 and 6).
    clock[0] += 5
    assert a.pod()["rates"][live.ELEVENLABS_REQUESTS]["peak_per_second"]["5m"] == 10
    # Two hours later the per-minute counts are gone; since-start keeps 10.
    clock[0] += 2 * 3600
    r = a.pod()["rates"][live.ELEVENLABS_REQUESTS]
    assert r["peak_per_minute"]["60m"] == 0
    assert r["peak_per_minute"]["since_start"] == 10
    assert r["peak_per_second"]["since_start"] == 10
    a.stop()


# ---------------------------------------------------------------------------
# ElevenLabs requests come from the socket / HTTP in-flight events
# ---------------------------------------------------------------------------


def test_sentences_to_elevenlabs_count_either_way(clock):
    st = LiveStats()
    st.add(live.WS_IN_FLIGHT, MODEL, None, 1)
    st.add(live.HTTP_IN_FLIGHT, "eleven_flash_v2_5", None, 1)
    assert st.current(live.ELEVENLABS_IN_FLIGHT) == 2
    st.add(live.WS_IN_FLIGHT, MODEL, None, -1)
    st.add(live.HTTP_IN_FLIGHT, "eleven_flash_v2_5", None, -1)
    assert st.current(live.ELEVENLABS_IN_FLIGHT) == 0

    pod = aggregate([st.snapshot()], clock[0])
    assert pod["rates"][live.ELEVENLABS_REQUESTS]["total"] == 2  # +1s only
    assert pod["rates"][live.ELEVENLABS_WEBSOCKET]["total"] == 1
    assert pod["rates"][live.ELEVENLABS_HTTP]["total"] == 1
    assert pod["in_flight"]["elevenlabs"] == 0
    mine = pod["workers"][0]["peaks"][live.ELEVENLABS_IN_FLIGHT]
    assert mine["since_start"] == 2


# ---------------------------------------------------------------------------
# Incoming requests: in flight until the last byte
# ---------------------------------------------------------------------------


async def _call(path: str, gate: asyncio.Event, seen: list[int]) -> None:
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"a", "more_body": True})
        seen.append(LIVE.current(live.REQUESTS_IN_FLIGHT))
        await gate.wait()  # still streaming
        await send({"type": "http.response.body", "body": b"b", "more_body": False})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    mw = InflightTrackingMiddleware(app)
    await mw({"type": "http", "path": path, "method": "POST"}, receive, send)


@pytest.mark.parametrize("path, counted", [("/tts/stream", 1), ("/health", 0)])
async def test_a_synthesis_request_is_in_flight_until_its_last_byte(path, counted):
    before = LIVE.current(live.REQUESTS_IN_FLIGHT)
    total0 = LIVE._rates[live.REQUESTS].total
    gate, seen = asyncio.Event(), []
    task = asyncio.create_task(_call(path, gate, seen))
    while not seen:
        await asyncio.sleep(0)
    assert seen[0] == before + counted  # mid-stream
    gate.set()
    await task
    assert LIVE.current(live.REQUESTS_IN_FLIGHT) == before
    assert LIVE._rates[live.REQUESTS].total == total0 + counted


async def test_a_metrics_failure_never_fails_a_request(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(LIVE, "level", boom)
    gate, seen = asyncio.Event(), []
    gate.set()
    await _call("/tts/bytes", gate, seen)  # must not raise
    assert seen


# ---------------------------------------------------------------------------
# Cache hits / misses through the real cache
# ---------------------------------------------------------------------------


class _Provider(BaseTTSProvider):
    name = "cartesia"
    native_sample_rate = 16000

    async def synth(self, *, text, voice_id, model, language, params) -> AudioResult:
        return AudioResult(
            audio=b"\x01\x02" * 800,
            container="raw",
            encoding="pcm_s16le",
            sample_rate=16000,
        )


def _req(text: str) -> TTSRequest:
    return TTSRequest(
        model_id="cartesia:sonic-2",
        transcript=text,
        voice=CartesiaVoice(id="v"),
        language="en",
        output_format=OutputFormat(encoding="pcm_s16le", sample_rate=16000),
    )


@pytest.mark.parametrize("write_behind", [True, False])
async def test_cache_hits_and_misses_are_counted(write_behind, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "metrics_write_behind_enabled", write_behind)
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    provider = _Provider()
    svc = CacheService(meta, blobs, lambda n: provider if n == "cartesia" else None)
    await svc.start()
    hits0 = LIVE._rates[live.CACHE_HITS].total
    misses0 = LIVE._rates[live.CACHE_MISSES].total
    try:
        headers, body = await svc.stream(_req("hello there friend"))
        b"".join([c async for c in body])
        assert headers["X-Cache"] == "MISS"
        for _ in range(3):
            headers, body = await svc.stream(_req("hello there friend"))
            b"".join([c async for c in body])
            assert headers["X-Cache"] == "HIT"
        assert LIVE._rates[live.CACHE_MISSES].total == misses0 + 1
        assert LIVE._rates[live.CACHE_HITS].total == hits0 + 3
    finally:
        await svc.stop()
    # ... and the durable stats were still recorded underneath.
    summary = await meta.provider_metrics_summary()
    assert summary  # forwarded to the real sink


async def test_a_metrics_failure_never_fails_a_cache_hit(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "metrics_write_behind_enabled", False)
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    provider = _Provider()
    svc = CacheService(meta, blobs, lambda n: provider if n == "cartesia" else None)

    def boom(*a, **k):
        raise RuntimeError("metrics down")

    monkeypatch.setattr(LIVE, "event", boom)
    for status in ("MISS", "HIT"):
        headers, body = await svc.stream(_req("one more line"))
        assert len(b"".join([c async for c in body])) == 1600
        assert headers["X-Cache"] == status


def test_hit_rate_per_window(clock):
    st = LiveStats()
    st.event(live.CACHE_HITS, 3)
    st.event(live.CACHE_MISSES, 1)
    pod = aggregate([st.snapshot()], clock[0])
    assert pod["cache"]["hit_rate_pct"]["5m"] == 75.0
    assert pod["cache"]["hit_rate_pct"]["since_start"] == 75.0
    assert (
        aggregate([LiveStats().snapshot()], clock[0])["cache"]["hit_rate_pct"]["5m"]
        is None
    )  # nothing served yet
