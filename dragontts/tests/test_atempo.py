"""ffmpeg atempo tempo stage — scoping, bypass, wiring, and cache keying.

Pinned behaviors:
- tempo applies ONLY to eleven_v3_conversational; every other model ignores
  it and its cache key never includes it (CacheService._resolve strips it);
- tempo == 1.0 (absent, default, or explicit) NEVER spawns ffmpeg;
- batch (_synth_v3) and streaming (stream_synth) produce the same stretched
  bytes, which is what cache-during-stream stores;
- spawn failure degrades to passthrough; upstream pool errors still propagate
  so stream_synth's one-shot fallback keeps working;
- params.tempo outside 0.5-2.0 fails schema validation.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from pydantic import ValidationError

from app.audio import atempo as atempo_mod
from app.audio.atempo import atempo_available, atempo_bytes, atempo_stream
from app.cache.key import canonical_params, hash_key
from app.cache.service import CacheService
from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeConnect

V3CONV = "eleven_v3_conversational"
needs_ffmpeg = pytest.mark.skipif(not atempo_available(), reason="ffmpeg not on PATH")


@pytest.fixture(autouse=True)
def _pin_v3_native_rate(monkeypatch):
    # These tests were written against telephony-native 8 kHz fixtures; the
    # deployment .env may raise the native rate (44.1 kHz). Pin it so they
    # exercise the tempo mechanism, not the ambient config.
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 8000)


def _sine_pcm(seconds: float = 1.0, rate: int = 8000) -> bytes:
    """Deterministic s16le mono PCM — alternating ramp frames, no numpy dep."""
    n = int(seconds * rate)
    return b"".join(((i % 256) * 257).to_bytes(2, "little") for i in range(n))


# -- atempo module ---------------------------------------------------------


@needs_ffmpeg
async def test_atempo_bytes_duration_factor():
    pcm = _sine_pcm(1.0)
    stretched = await atempo_bytes(pcm, 8000, 2.0)
    assert len(stretched) == pytest.approx(len(pcm) / 2.0, rel=0.15)
    slower = await atempo_bytes(pcm, 8000, 0.8)
    assert len(slower) == pytest.approx(len(pcm) / 0.8, rel=0.15)


@needs_ffmpeg
async def test_streaming_matches_one_shot():
    pcm = _sine_pcm(1.0)
    chunks = [pcm[i : i + 777] for i in range(0, len(pcm), 777)]

    async def source():
        for c in chunks:
            yield c

    streamed = b"".join([c async for c in atempo_stream(source(), 8000, 1.3)])
    oneshot = await atempo_bytes(pcm, 8000, 1.3)
    assert streamed == oneshot


async def test_passthrough_when_spawn_fails(monkeypatch):
    async def _no_spawn(sample_rate, tempo):
        return None

    monkeypatch.setattr(atempo_mod, "_spawn", _no_spawn)

    async def source():
        yield b"abc"
        yield b"def"

    out = b"".join([c async for c in atempo_stream(source(), 8000, 1.5)])
    assert out == b"abcdef"


async def test_upstream_errors_propagate(monkeypatch):
    from app.providers.base import ProviderError

    async def source():
        yield b"partial"
        raise ProviderError("pool exploded")

    with pytest.raises(ProviderError, match="pool exploded"):
        async for _ in atempo_stream(source(), 8000, 1.5):
            pass


# -- scope + bypass (_tempo_for) -------------------------------------------


def _scoped_provider() -> ElevenLabsProvider:
    return ElevenLabsProvider(api_key="k", base_url=BASE)


def test_tempo_scoped_to_v3conv_only():
    p = _scoped_provider()
    # Non-v3conv models NEVER stretch, no matter what params carry.
    assert p._tempo_for({"tempo": 1.15}, "eleven_v3") == 1.0
    assert p._tempo_for({"tempo": 1.15}, "eleven_flash_v2_5") == 1.0
    assert p._tempo_for({"tempo": 1.15}, None) == 1.0
    # v3conv without a tempo param falls back to the (default 1.0) knob.
    assert p._tempo_for({}, V3CONV) == 1.0


def test_tempo_one_is_a_pure_bypass():
    p = _scoped_provider()
    assert p._tempo_for({"tempo": 1.0}, V3CONV) == 1.0
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "elevenlabs_v3conv_default_tempo", 1.15)
    try:
        p2 = _scoped_provider()
        assert p2._tempo_for({"tempo": 1.0}, V3CONV) == 1.0  # explicit 1.0 wins
        assert p2._tempo_for({}, V3CONV) == 1.15  # knob default applies
    finally:
        monkey.undo()


def test_tempo_killswitch_and_range(monkeypatch):
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    p = _scoped_provider()
    assert p._tempo_for({"tempo": 1.15}, V3CONV) == 1.15
    monkeypatch.setattr(settings, "elevenlabs_atempo_enabled", False)
    assert p._tempo_for({"tempo": 1.15}, V3CONV) == 1.0
    monkeypatch.setattr(settings, "elevenlabs_atempo_enabled", True)
    # Out-of-range values degrade to 1.0 (schema 400s them first in practice).
    assert p._tempo_for({"tempo": 2.5}, V3CONV) == 1.0
    assert p._tempo_for({"tempo": "garbage"}, V3CONV) == 1.0
    # ffmpeg missing from PATH: passthrough, not failure.
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: False)
    assert p._tempo_for({"tempo": 1.15}, V3CONV) == 1.0


# -- provider wiring --------------------------------------------------------


def _install_fake_connect(monkeypatch, connect: FakeConnect) -> None:
    def _fake_connect(uri, additional_headers=None, open_timeout=None):
        return connect(uri, additional_headers or {})

    monkeypatch.setattr(elevenlabs_pool, "connect", _fake_connect)


class _NoHTTPClient:
    async def post(self, *args, **kwargs):
        raise AssertionError("eleven_v3 must not hit the HTTP text-to-speech endpoint")

    async def aclose(self):
        return None


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            if predicate():
                return
        except (IndexError, AttributeError, StopIteration):
            pass
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


def _feed_utterance(socket, ctx_id: str, pcm: bytes) -> None:
    for i in range(0, len(pcm), 4000):
        part = pcm[i : i + 4000]
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(part).decode()})
    socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})


@needs_ffmpeg
async def test_synth_v3conv_batch_applies_tempo(monkeypatch):
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = _sine_pcm(1.0)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    try:
        synth = asyncio.create_task(
            provider.synth(
                text="hello",
                voice_id=VOICE,
                model=V3CONV,
                language=None,
                params={"tempo": 2.0, "stability": 0.4},
            )
        )
        await _wait_for(lambda: connect.sockets)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        _feed_utterance(socket, ctx_id, pcm)
        result = await asyncio.wait_for(synth, timeout=5.0)

        assert result.sample_rate == settings.elevenlabs_v3_native_sample_rate == 8000
        # Stretched BY the provider, so the cache stores the stretched clip.
        assert len(result.audio) == pytest.approx(len(pcm) / 2.0, rel=0.15)
    finally:
        await provider.aclose()


@needs_ffmpeg
async def test_synth_v3conv_tempo_one_never_spawns_ffmpeg(monkeypatch):
    """Explicit tempo == 1.0 must bypass ffmpeg entirely — atempo_bytes is
    patched to explode if the provider tries to stretch."""

    async def _explode(*args, **kwargs):
        raise AssertionError("tempo 1.0 must never spawn ffmpeg")

    monkeypatch.setattr("app.providers.elevenlabs.atempo_bytes", _explode)
    pcm = b"x" * 8000
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    try:
        synth = asyncio.create_task(
            provider.synth(
                text="hello",
                voice_id=VOICE,
                model=V3CONV,
                language=None,
                params={"tempo": 1.0},
            )
        )
        await _wait_for(lambda: connect.sockets)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        _feed_utterance(socket, ctx_id, pcm)
        result = await asyncio.wait_for(synth, timeout=5.0)
        assert result.audio == pcm  # byte-identical: no ffmpeg in the path
    finally:
        await provider.aclose()


@needs_ffmpeg
async def test_synth_plain_v3_ignores_tempo_param(monkeypatch):
    """Non-v3conv model: a tempo param must not stretch (scope test) at the
    provider level, mirroring the cache-layer strip."""

    async def _explode(*args, **kwargs):
        raise AssertionError("tempo must never apply to plain eleven_v3")

    monkeypatch.setattr("app.providers.elevenlabs.atempo_bytes", _explode)
    pcm = b"y" * 8000
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    try:
        synth = asyncio.create_task(
            provider.synth(
                text="hello",
                voice_id=VOICE,
                model=V3_MODEL,
                language=None,
                params={"tempo": 1.5},
            )
        )
        await _wait_for(lambda: connect.sockets)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        _feed_utterance(socket, ctx_id, pcm)
        result = await asyncio.wait_for(synth, timeout=5.0)
        assert result.audio == pcm
    finally:
        await provider.aclose()


@needs_ffmpeg
async def test_stream_synth_v3conv_applies_tempo(monkeypatch):
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = _sine_pcm(1.0)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    try:

        async def consume():
            return b"".join(
                [
                    c
                    async for c in provider.stream_synth(
                        text="hello",
                        voice_id=VOICE,
                        model=V3CONV,
                        language=None,
                        params={"tempo": 2.0},
                    )
                ]
            )

        task = asyncio.create_task(consume())
        await _wait_for(lambda: connect.sockets)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        _feed_utterance(socket, ctx_id, pcm)
        out = await asyncio.wait_for(task, timeout=5.0)

        # Streamed output == batch output for the same tempo: cache-during-
        # stream stores exactly what the caller heard.
        batch = await atempo_bytes(pcm, 8000, 2.0)
        assert out == batch
        assert len(out) == pytest.approx(len(pcm) / 2.0, rel=0.15)
    finally:
        await provider.aclose()


# -- cache keying + schema ---------------------------------------------------


def test_canonical_params_tempo_keying():
    # tempo 1.0 collapses with "absent" (PROVIDER_DEFAULTS entry) — one key.
    assert canonical_params("elevenlabs", {"tempo": 1.0}) == canonical_params(
        "elevenlabs", {}
    )
    # A real tempo gets its own canonical entry → its own cache key.
    assert canonical_params("elevenlabs", {"tempo": 1.15}) != canonical_params(
        "elevenlabs", {}
    )


def _resolve_key(
    model: str, params: dict, encoding: str = "pcm_s16le", sample_rate: int = 8000
) -> tuple[str, dict]:
    svc = object.__new__(CacheService)  # _resolve uses no instance state
    req = TTSRequest(
        model_id=f"elevenlabs:{model}",
        transcript="hello there",
        voice=CartesiaVoice(id=VOICE),
        language="en",
        output_format=OutputFormat(encoding=encoding, sample_rate=sample_rate),
        params=params,
    )
    provider, model_out, of, canon, key = svc._resolve(req)
    return key, req.params


def test_v3conv_keys_separate_model_variant_tempo_and_format():
    # Model variants, tempo, and the output format each get their own key.
    keys = {
        _resolve_key(V3CONV, {})[0],
        _resolve_key(V3CONV, {"tempo": 1.2})[0],
        _resolve_key(V3CONV + "_tempo", {"tempo": 1.2})[0],
        _resolve_key(V3CONV + "_clean_tempo", {"tempo": 1.2})[0],
    }
    assert len(keys) == 4
    # The output format rides in the v3conv key (stored bytes ARE the final
    # result per format)...
    assert (
        _resolve_key(V3CONV, {}, encoding="pcm_mulaw", sample_rate=8000)[0]
        != _resolve_key(V3CONV, {}, encoding="pcm_s16le", sample_rate=8000)[0]
    )
    # ...but NOT in other models' keys (native-store, format-agnostic).
    assert (
        _resolve_key("eleven_flash_v2_5", {}, encoding="pcm_mulaw", sample_rate=8000)[0]
        == _resolve_key(
            "eleven_flash_v2_5", {}, encoding="pcm_s16le", sample_rate=16000
        )[0]
    )


def test_resolve_strips_tempo_for_non_v3conv():
    # Same transcript+voice+model: tempo-bearing and tempo-free requests for
    # plain eleven_v3 collapse to ONE key, and the param is gone downstream.
    key_plain, params_plain = _resolve_key("eleven_v3", {})
    key_tempo, params_tempo = _resolve_key("eleven_v3", {"tempo": 1.15})
    assert key_plain == key_tempo
    assert "tempo" not in params_tempo


def test_resolve_keeps_tempo_for_v3conv():
    key_plain, _ = _resolve_key(V3CONV, {})
    key_tempo, params_tempo = _resolve_key(V3CONV, {"tempo": 1.15})
    assert key_plain != key_tempo
    assert params_tempo["tempo"] == 1.15
    # Explicit 1.0 keeps the default key (collapses in canonical_params).
    key_one, params_one = _resolve_key(V3CONV, {"tempo": 1.0})
    assert key_one == key_plain
    assert params_one == {"tempo": 1.0}  # kept (it's the model's param), harmless


def test_resolve_normalizes_speed_to_tempo_for_v3conv():
    """The legacy `speed` param (what clairvoyance templates already send) is
    aliased into `tempo` for eleven_v3_conversational — zero client changes.
    Both spellings land on ONE cache entry; tempo wins when both are sent."""
    key_speed, params_speed = _resolve_key(V3CONV, {"speed": 1.15})
    key_tempo, _ = _resolve_key(V3CONV, {"tempo": 1.15})
    assert key_speed == key_tempo
    assert params_speed == {"tempo": 1.15}  # speed is gone, tempo carries it

    # Explicit tempo wins over speed (speed is dropped, not double-keyed).
    _, params_both = _resolve_key(V3CONV, {"speed": 1.2, "tempo": 1.15})
    assert params_both == {"tempo": 1.15}

    # speed=1.0 normalizes to tempo=1.0 -> default key, no ffmpeg.
    key_speed_one, params_speed_one = _resolve_key(V3CONV, {"speed": 1.0})
    key_plain, _ = _resolve_key(V3CONV, {})
    assert key_speed_one == key_plain
    assert params_speed_one == {"tempo": 1.0}

    # Other models keep speed as speed (flash honors it natively).
    _, params_flash = _resolve_key("eleven_flash_v2_5", {"speed": 1.2})
    assert params_flash == {"speed": 1.2}


def test_schema_rejects_out_of_range_tempo():
    with pytest.raises(ValidationError):
        TTSRequest(
            model_id=f"elevenlabs:{V3CONV}",
            transcript="hi",
            voice=CartesiaVoice(id=VOICE),
            params={"tempo": 2.5},
        )
    with pytest.raises(ValidationError):
        TTSRequest(
            model_id=f"elevenlabs:{V3CONV}",
            transcript="hi",
            voice=CartesiaVoice(id=VOICE),
            params={"tempo": "fast"},
        )
    req = TTSRequest(
        model_id=f"elevenlabs:{V3CONV}",
        transcript="hi",
        voice=CartesiaVoice(id=VOICE),
        params={"tempo": 1.15},
    )
    assert req.params["tempo"] == 1.15


# -- presence EQ (spark recovery for the 8 kHz band limit) -------------------


def test_presence_boost_shapes_band():
    import numpy as np

    from app.audio.format import apply_presence_boost

    rate = 44100

    def _tone(freq: float) -> bytes:
        t = np.arange(rate // 2) / rate  # 0.5 s
        return (12000 * np.sin(2 * np.pi * freq * t)).astype("<i2").tobytes()

    def _peak(pcm: bytes) -> float:
        return float(np.max(np.abs(np.frombuffer(pcm, dtype="<i2"))))

    # Zero boost is a byte-identical no-op.
    assert apply_presence_boost(_tone(2800), rate, 0.0) == _tone(2800)
    # +6 dB ≈ x2 at the 2.8 kHz peak; out-of-band (500 Hz) untouched.
    boosted = apply_presence_boost(_tone(2800), rate, 6.0206)
    assert _peak(boosted) == pytest.approx(2 * 12000, rel=0.05)
    low = apply_presence_boost(_tone(500), rate, 6.0206)
    assert _peak(low) == pytest.approx(12000, rel=0.05)


# -- cache lifecycle: stretch ONCE at gen time, serve the stored stretched
#    blob forever — ffmpeg never runs again for that entry -------------------


async def _lifecycle_svc(tmp_path, provider, monkeypatch):
    from app.cache.service import CacheService
    from app.storage.filesystem import FilesystemBlobStore
    from app.storage.sqlite import SQLiteMetadataStore

    monkeypatch.setattr(settings, "metrics_write_behind_enabled", False)
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    svc = CacheService(
        meta, blobs, lambda name: provider if name == "elevenlabs" else None
    )
    return svc


def _tempo_req(tempo: float) -> TTSRequest:
    return TTSRequest(
        model_id=f"elevenlabs:{V3CONV}",
        transcript="hello there friend",
        voice=CartesiaVoice(id=VOICE),
        language="en",
        output_format=OutputFormat(encoding="pcm_s16le", sample_rate=8000),
        params={"tempo": tempo} if tempo != 1.0 else {},
    )


def _utterance_flushes(connect: FakeConnect) -> int:
    return sum(
        1
        for s in connect.sockets
        for m in s.sent
        if m.get("flush") and m.get("context_id") != "dragontts-keepalive"
    )


_fed_ctx: set[str] = set()


async def _drive_one_utterance(connect: FakeConnect, pcm: bytes) -> None:
    """Feed the NEXT utterance the pool sends, on whichever socket carries it.

    Warm-socket reuse means later requests don't open a new socket, and a
    reused socket's ``sent`` already holds earlier flushes — so wait for a
    flush whose context we haven't fed yet instead of grabbing the first.
    """

    def _unfed():
        return [
            (s, m["context_id"])
            for s in connect.sockets
            for m in s.sent
            if m.get("flush")
            and m.get("context_id") != "dragontts-keepalive"
            and m["context_id"] not in _fed_ctx
        ]

    await _wait_for(lambda: _unfed())
    socket, ctx_id = _unfed()[0]
    _fed_ctx.add(ctx_id)
    _feed_utterance(socket, ctx_id, pcm)


@needs_ffmpeg
async def test_tempo_stretched_once_then_served_from_cache(monkeypatch, tmp_path):
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = _sine_pcm(1.0)
    expected = await atempo_bytes(pcm, 8000, 1.15)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    svc = await _lifecycle_svc(tmp_path, provider, monkeypatch)
    try:
        # MISS: one synthesis, stretched by the provider, stored stretched.
        task = asyncio.create_task(svc.get_or_synthesize(_tempo_req(1.15)))
        await _drive_one_utterance(connect, pcm)
        audio1, _ = await asyncio.wait_for(task, timeout=5.0)
        assert audio1 == expected
        flushes_after_miss = _utterance_flushes(connect)

        # HIT: byte-identical stretched audio, ZERO new synthesis (no ffmpeg,
        # no TTD utterance — the stored blob is served as-is).
        audio2, _ = await asyncio.wait_for(
            svc.get_or_synthesize(_tempo_req(1.15)), timeout=5.0
        )
        assert audio2 == audio1
        assert _utterance_flushes(connect) == flushes_after_miss

        # Different tempo = different key = its own synthesis (unstretched).
        task = asyncio.create_task(svc.get_or_synthesize(_tempo_req(1.0)))
        await _drive_one_utterance(connect, pcm)
        audio0, _ = await asyncio.wait_for(task, timeout=5.0)
        assert audio0 == pcm  # tempo 1.0: bypass, byte-identical to source
    finally:
        await provider.aclose()


@needs_ffmpeg
async def test_stream_tempo_hit_serves_stored_stretched(monkeypatch, tmp_path):
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = _sine_pcm(1.0)
    expected = await atempo_bytes(pcm, 8000, 1.15)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    svc = await _lifecycle_svc(tmp_path, provider, monkeypatch)
    try:

        async def consume_miss():
            _, body = await svc.stream(_tempo_req(1.15))
            return b"".join([c async for c in body])

        # Streaming MISS: chunks pass through the live atempo pipe AND the
        # accumulated stretched bytes are stored by cache-during-stream.
        task = asyncio.create_task(consume_miss())
        await _drive_one_utterance(connect, pcm)
        out1 = await asyncio.wait_for(task, timeout=5.0)
        assert out1 == expected
        flushes_after_miss = _utterance_flushes(connect)

        # Streaming HIT: the stored stretched blob is served chunked — no new
        # utterance, no ffmpeg.
        _, body = await svc.stream(_tempo_req(1.15))
        out2 = b"".join([c async for c in body])
        assert out2 == out1
        assert _utterance_flushes(connect) == flushes_after_miss
    finally:
        await provider.aclose()


@needs_ffmpeg
async def test_variant_cache_stores_final_converted_result(monkeypatch, tmp_path):
    """The v3conv cache stores the END RESULT in the caller's format: a MISS
    synthesizes at 44.1 kHz, stretches, then does ONE sinc downsample + μ-law
    encode and stores THAT; the HIT serves identical bytes with zero
    processing (no ffmpeg, no resample, no re-encode)."""
    from app.audio.format import convert_audio

    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = _sine_pcm(1.0, 44100)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    svc = await _lifecycle_svc(tmp_path, provider, monkeypatch)

    def _req() -> TTSRequest:
        return TTSRequest(
            model_id=f"elevenlabs:{V3CONV}_tempo",
            transcript="hello there friend",
            voice=CartesiaVoice(id=VOICE),
            language="en",
            output_format=OutputFormat(encoding="pcm_mulaw", sample_rate=8000),
            params={"tempo": 1.15},
        )

    try:
        task = asyncio.create_task(svc.get_or_synthesize(_req()))
        await _drive_one_utterance(connect, pcm)
        audio1, h1 = await asyncio.wait_for(task, timeout=10.0)
        flushes_after_miss = _utterance_flushes(connect)

        # End result computed independently: atempo at 44.1k, then one
        # convert to μ-law 8 kHz.
        stretched = await atempo_bytes(pcm, 44100, 1.15)
        expected = convert_audio(
            stretched,
            native_encoding="pcm_s16le",
            native_rate=44100,
            out_encoding="pcm_mulaw",
            out_rate=8000,
        )
        assert audio1 == expected

        # The STORED record carries the final format, not the 44.1k
        # intermediate — hits never resample it again.
        rec = await svc._metadata.get(h1["X-Cache-Key"])
        assert (rec.encoding, rec.sample_rate) == ("pcm_mulaw", 8000)

        # HIT: byte-identical, zero new synthesis.
        audio2, _ = await asyncio.wait_for(svc.get_or_synthesize(_req()), timeout=5.0)
        assert audio2 == audio1
        assert _utterance_flushes(connect) == flushes_after_miss
    finally:
        await provider.aclose()


# -- tempo 1.0 => direct pcm_8000, unaltered --------------------------------


async def _synth_v3conv(provider, params, connect, pcm, model=V3CONV):
    synth = asyncio.create_task(
        provider.synth(
            text="hello",
            voice_id=VOICE,
            model=model,
            language=None,
            params=params,
        )
    )
    await _drive_one_utterance(connect, pcm)
    return await asyncio.wait_for(synth, timeout=5.0)


async def test_speed1_returns_direct_pcm8000_unaltered(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    # PCM with heavy silence pads: proves hygiene is skipped on this path.
    pcm = b"\x00\x00" * 8000 + _sine_pcm(1.0) + b"\x00\x00" * 8000
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(
        provider, {"speed": 1.0, "stability": 0.4}, connect, pcm
    )

    assert "output_format=pcm_8000" in connect.uris[0]
    assert result.sample_rate == 8000
    assert result.audio == pcm  # byte-for-byte what ElevenLabs sent


@needs_ffmpeg
async def test_base_speed12_is_pcm8000_plus_atempo_no_hygiene(monkeypatch):
    """Base model, speed != 1: ElevenLabs' own pcm_8000 with ONLY the atempo
    stretch on top — pads survive (hygiene never runs on this path)."""
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = b"\x00\x00" * 8000 + _sine_pcm(1.0) + b"\x00\x00" * 8000
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(provider, {"tempo": 2.0}, connect, pcm)

    assert "output_format=pcm_8000" in connect.uris[0]  # still their 8k, not 44.1k
    assert result.sample_rate == 8000
    expected = await atempo_bytes(pcm, 8000, 2.0)
    assert result.audio == expected  # padded input stretched whole: no clean


async def test_base_speeds_share_8k_pool_variants_get_full_band(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()

    # The provider speaks params.tempo; the cache layer aliases speed->tempo
    # before the provider ever sees the request.
    await _synth_v3conv(provider, {"tempo": 1.0}, connect, _sine_pcm(0.5))
    await _synth_v3conv(provider, {"tempo": 1.2}, connect, _sine_pcm(0.5))

    # Base model at ANY speed: one shared pcm_8000 pool — the second synth
    # reuses the warm socket, so exactly one connect URI so far.
    rates = [u.split("output_format=pcm_")[1].split("&")[0] for u in connect.uris]
    assert rates == ["8000"]
    assert len(provider._pools) == 1

    # The full-band variant opens its own 44.1k pool and speaks the NORMALIZED
    # model id upstream (ElevenLabs doesn't know the _tempo suffix).
    await _synth_v3conv(
        provider,
        {"tempo": 1.2},
        connect,
        _sine_pcm(0.5, 44100),
        model=V3CONV + "_tempo",
    )
    assert "output_format=pcm_44100" in connect.uris[-1]
    assert "model_id=eleven_v3_conversational&" in connect.uris[-1]
    assert len(provider._pools) == 2


async def test_synth_native_format_is_variant_aware(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    # Base model: ElevenLabs' own pcm_8000 at ANY speed (tempo only stretches).
    assert provider.synth_native_format(V3CONV, {"tempo": 1.0}) == ("pcm_s16le", 8000)
    assert provider.synth_native_format(V3CONV, {"tempo": 1.2}) == ("pcm_s16le", 8000)
    assert provider.synth_native_format(V3CONV) == ("pcm_s16le", 8000)
    # Full-band variants at any speed.
    for m in (V3CONV + "_tempo", V3CONV + "_clean_tempo"):
        assert provider.synth_native_format(m, {"tempo": 1.2}) == ("pcm_s16le", 44100)
        assert provider.synth_native_format(m, {}) == ("pcm_s16le", 44100)
    # Knob off: the base model runs the full-band chain instead.
    monkeypatch.setattr(settings, "elevenlabs_tempo1_direct_pcm8000", False)
    assert provider.synth_native_format(V3CONV, {"tempo": 1.0}) == ("pcm_s16le", 44100)


@needs_ffmpeg
async def test_tempo_variant_full_band_no_hygiene(monkeypatch):
    """_tempo variant: pcm_44100 from the pool, atempo applied, NO hygiene."""
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = b"\x00\x00" * 4410 + _sine_pcm(1.0, 44100) + b"\x00\x00" * 4410
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(
        provider, {"tempo": 2.0}, connect, pcm, model=V3CONV + "_tempo"
    )

    assert "output_format=pcm_44100" in connect.uris[0]
    assert result.sample_rate == 44100
    expected = await atempo_bytes(pcm, 44100, 2.0)
    assert result.audio == expected  # pads intact: hygiene never ran


@needs_ffmpeg
async def test_clean_tempo_variant_hygiene_then_atempo(monkeypatch):
    """_clean_tempo variant: pcm_44100, hygiene (pads trimmed) then atempo."""
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    pcm = b"\x00\x00" * 4410 + _sine_pcm(1.0, 44100) + b"\x00\x00" * 4410
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(
        provider, {"tempo": 2.0}, connect, pcm, model=V3CONV + "_clean_tempo"
    )

    assert "output_format=pcm_44100" in connect.uris[0]
    assert result.sample_rate == 44100
    stretched_raw = await atempo_bytes(pcm, 44100, 2.0)
    assert len(result.audio) < len(stretched_raw)  # pads were cleaned first
    # Cleaned clip = speech + lead/tail caps (60/120 ms), then halved by 2.0.
    assert len(result.audio) / 2 / 44100 == pytest.approx(
        (1.0 + 0.060 + 0.120) / 2.0, rel=0.1
    )


async def test_direct8k_knob_off_keeps_processing_chain(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    monkeypatch.setattr(settings, "elevenlabs_tempo1_direct_pcm8000", False)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    result = await _synth_v3conv(provider, {"speed": 1.0}, connect, _sine_pcm(1.0))

    assert "output_format=pcm_44100" in connect.uris[0]
    assert result.sample_rate == 44100
    assert len(result.audio) < len(_sine_pcm(1.0))  # hygiene trimmed the pads
