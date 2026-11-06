"""Local pipeline variants for eleven_flash_v2_5.

``eleven_flash_v2_5_tempo`` / ``_clean_tempo`` / ``_clean_tempo_v2`` run the
same local chain as the v3 / v4 variants on the finished flash clip; the BARE
``eleven_flash_v2_5`` must keep today's path exactly (same request, same bytes,
format-agnostic cache key, live streaming). Flash honors ElevenLabs' own
``speed``, so its variants keep speed native — only an explicit ``tempo`` adds
the atempo stretch. Fake HTTP / sockets, no network.
"""

from __future__ import annotations

import asyncio
import base64

import numpy as np
import pytest

from app.audio.join import speech_level_dbfs
from app.cache.service import CacheService
from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.base import AudioResult
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import (
    has_local_pipeline,
    is_flash_pipeline_model,
    needs_whole_clip,
    normalize_pipeline_model,
    pipeline_variant,
)
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore
from tests.test_atempo import needs_ffmpeg
from tests.test_elevenlabs_v3 import BASE, VOICE, FakeConnect, _wait_for

FLASH = "eleven_flash_v2_5"
RATE = 16000


def _speech_with_pads(amp: float = 2500.0) -> bytes:
    """1 s of syllable-shaped voice between 300 ms pads, at flash's 16 kHz."""
    t = np.arange(RATE) / RATE
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 2.5 * t) ** 2
    voice = (amp * env * np.sin(2 * np.pi * 220 * t)).astype("<i2").tobytes()
    pad = b"\x00\x00" * int(0.3 * RATE)
    return pad + voice + pad


class RecordingHTTP:
    """Stands in for the provider's httpx client: records each POST and
    answers with ``audio`` like the /v1/text-to-speech endpoint."""

    def __init__(self, audio: bytes) -> None:
        self.audio = audio
        self.posts: list[dict] = []

    async def post(self, url, json=None, headers=None):
        self.posts.append({"url": url, "json": json})
        audio = self.audio

        class _Resp:
            content = audio

            def raise_for_status(self):
                return None

        return _Resp()

    async def aclose(self):
        return None


async def _synth(model: str, params: dict, raw: bytes):
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    http = RecordingHTTP(raw)
    setattr(provider, "_client", http)  # stand-in httpx client
    try:
        result = await provider.synth(
            text="नमस्ते", voice_id=VOICE, model=model, language="hi-IN", params=params
        )
    finally:
        await provider.aclose()
    return result, http.posts[0]["json"]


# ---------------------------------------------------------------------------
# Model names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, variant",
    [
        (FLASH + "_tempo", "tempo"),
        (FLASH + "_clean_tempo", "clean_tempo"),
        (FLASH + "_clean_tempo_v2", "clean_tempo_v2"),
    ],
)
def test_flash_variant_names(model, variant):
    assert has_local_pipeline(model) and is_flash_pipeline_model(model)
    assert pipeline_variant(model) == variant
    assert normalize_pipeline_model(model) == FLASH
    assert needs_whole_clip(model) == (variant != "tempo")


@pytest.mark.parametrize(
    "model", [FLASH, FLASH + "_foo", "eleven_flash_v2", "eleven_turbo_v2_5_tempo"]
)
def test_other_flash_ids_have_no_pipeline(model):
    """The bare id keeps today's path; unknown suffixes / other models too."""
    assert not has_local_pipeline(model)
    assert not is_flash_pipeline_model(model)
    assert normalize_pipeline_model(model) == model


def test_v3_v4_names_unchanged():
    assert pipeline_variant("eleven_v3_conversational") == "base"
    assert pipeline_variant("eleven_v4_turbo_clean_tempo_v2") == "clean_tempo_v2"
    assert not is_flash_pipeline_model("eleven_v3_conversational_clean_tempo_v2")
    assert needs_whole_clip("eleven_v3_conversational_clean_tempo")
    assert not needs_whole_clip("eleven_v3_conversational_tempo")


# ---------------------------------------------------------------------------
# synth (the /tts/bytes path)
# ---------------------------------------------------------------------------


async def test_bare_flash_is_untouched():
    raw = _speech_with_pads()
    result, payload = await _synth(FLASH, {"speed": 1.1}, raw)
    assert result.audio == raw and result.sample_rate == RATE
    assert payload["model_id"] == FLASH
    assert payload["language_code"] == "hi"
    assert payload["voice_settings"]["speed"] == 1.1


@pytest.mark.parametrize("variant", ["_clean_tempo", "_clean_tempo_v2"])
async def test_flash_clean_variants_call_flash_and_run_the_chain(variant):
    raw = _speech_with_pads()
    result, payload = await _synth(
        FLASH + variant, {"speed": 1.16, "stability": 0.4}, raw
    )
    # ElevenLabs sees plain flash, with its native speed and language.
    assert payload["model_id"] == FLASH
    assert payload["language_code"] == "hi"
    assert payload["voice_settings"]["speed"] == 1.16
    assert payload["voice_settings"]["stability"] == 0.4
    # The chain ran: silent pads trimmed, clip shorter, still 16 kHz.
    assert result.sample_rate == RATE
    assert len(result.audio) < len(raw)
    assert len(result.audio) > RATE * 2 * 0.95, "the speech itself is kept"


@pytest.mark.parametrize("amp", [5000.0, 1500.0])  # a bit quiet / very quiet
async def test_clean_tempo_v2_evens_the_level_and_v1_does_not(amp):
    raw = _speech_with_pads(amp=amp)
    before = speech_level_dbfs(raw, RATE)
    assert before is not None
    v1, _ = await _synth(FLASH + "_clean_tempo", {}, raw)
    v2, _ = await _synth(FLASH + "_clean_tempo_v2", {}, raw)
    target = settings.elevenlabs_v2_level_target_dbfs
    # v2 lands on the target, but never lifts a clip by more than max_gain.
    expected = min(target, before + settings.elevenlabs_v2_level_max_gain_db)
    assert abs((speech_level_dbfs(v1.audio, RATE) or 0) - before) < 1.0
    assert abs((speech_level_dbfs(v2.audio, RATE) or 0) - expected) < 1.0
    assert expected - before > 3, "the fixture is quiet enough to show the change"


async def test_flash_tempo_variant_without_tempo_is_raw():
    raw = _speech_with_pads()
    result, payload = await _synth(FLASH + "_tempo", {"speed": 1.16}, raw)
    assert result.audio == raw, "speed is native on flash; no stretch without tempo"
    assert payload["voice_settings"]["speed"] == 1.16


@needs_ffmpeg
async def test_flash_tempo_variant_stretches_an_explicit_tempo():
    raw = _speech_with_pads()
    result, _ = await _synth(FLASH + "_tempo", {"tempo": 2.0}, raw)
    assert abs(len(result.audio) / len(raw) - 0.5) < 0.05


# ---------------------------------------------------------------------------
# stream_synth (the /tts/stream path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        FLASH + "_clean_tempo",
        FLASH + "_clean_tempo_v2",
        "eleven_v3_conversational_clean_tempo_v2",
        "eleven_v4_turbo_clean_tempo",
    ],
)
async def test_clean_variants_never_stream_raw(monkeypatch, model):
    """The chain needs the whole clip: the stream path yields the processed
    clip from synth() and never forwards raw socket chunks."""
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    done = AudioResult(
        audio=b"\x07\x00" * 100, container="raw", encoding="pcm_s16le", sample_rate=RATE
    )
    calls: list[str] = []

    async def fake_synth(**kw):
        calls.append(kw["model"])
        return done

    monkeypatch.setattr(provider, "synth", fake_synth)
    monkeypatch.setattr(provider, "_get_pool", lambda *a, **k: pytest.fail("no socket"))
    try:
        chunks = [
            c
            async for c in provider.stream_synth(
                text="hi", voice_id=VOICE, model=model, language="hi", params={}
            )
        ]
    finally:
        await provider.aclose()
    assert chunks == [done.audio] and calls == [model]


async def test_bare_flash_still_streams_live(monkeypatch):
    connect = FakeConnect()
    monkeypatch.setattr(
        elevenlabs_pool,
        "connect",
        lambda uri, additional_headers=None, open_timeout=None: connect(
            uri, additional_headers
        ),
    )
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    setattr(provider, "_client", RecordingHTTP(b""))  # must not be used
    got: list[bytes] = []

    async def consume():
        async for c in provider.stream_synth(
            text="hi", voice_id=VOICE, model=FLASH, language="hi", params={}
        ):
            got.append(c)

    try:
        task = asyncio.create_task(consume())
        await _wait_for(
            lambda: connect.sockets
            and any(m.get("flush") for m in connect.sockets[0].sent)
        )
        socket = connect.sockets[0]
        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed(
            {"contextId": ctx, "audio": base64.b64encode(b"\x01\x02" * 8).decode()}
        )
        socket.feed({"contextId": ctx, "isFinal": True})
        await asyncio.wait_for(task, 2.0)
        assert got == [b"\x01\x02" * 8]
        assert f"model_id={FLASH}" in connect.uris[0]
    finally:
        await provider.aclose()


async def test_flash_tempo_streams_live_through_atempo_at_16k(monkeypatch):
    """The live stretch must run at the rate the socket speaks — 16 kHz for
    flash (it used the v3 rate before, 8 kHz)."""
    monkeypatch.setattr("app.providers.elevenlabs.atempo_available", lambda: True)
    seen: dict = {}

    async def fake_atempo_stream(source, rate, tempo):
        seen["rate"], seen["tempo"] = rate, tempo
        async for c in source:
            yield c

    monkeypatch.setattr("app.providers.elevenlabs.atempo_stream", fake_atempo_stream)
    connect = FakeConnect()
    monkeypatch.setattr(
        elevenlabs_pool,
        "connect",
        lambda uri, additional_headers=None, open_timeout=None: connect(
            uri, additional_headers
        ),
    )
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)

    async def consume():
        return [
            c
            async for c in provider.stream_synth(
                text="hi",
                voice_id=VOICE,
                model=FLASH + "_tempo",
                language="hi",
                params={"tempo": 1.5},
            )
        ]

    try:
        task = asyncio.create_task(consume())
        await _wait_for(
            lambda: connect.sockets
            and any(m.get("flush") for m in connect.sockets[0].sent)
        )
        socket = connect.sockets[0]
        ctx = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed(
            {"contextId": ctx, "audio": base64.b64encode(b"\x01\x02" * 8).decode()}
        )
        socket.feed({"contextId": ctx, "isFinal": True})
        assert await asyncio.wait_for(task, 2.0) == [b"\x01\x02" * 8]
        assert seen == {"rate": 16000, "tempo": 1.5}
        assert f"model_id={FLASH}&" in connect.uris[0], "the socket speaks plain flash"
    finally:
        await provider.aclose()


# ---------------------------------------------------------------------------
# Cache keying
# ---------------------------------------------------------------------------


async def _resolve(tmp_path, model: str, params: dict, rate: int):
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    svc = CacheService(meta, blobs, lambda n: None)
    req = TTSRequest(
        model_id=f"elevenlabs:{model}",
        transcript="नमस्ते",
        voice=CartesiaVoice(id=VOICE),
        language="hi",
        output_format=OutputFormat(encoding="pcm_s16le", sample_rate=rate),
        params=dict(params),
    )
    _provider, _model, _of, params_canon, key = svc._resolve(req)
    return params_canon, key


async def test_flash_variant_keys_on_format_and_keeps_speed(tmp_path):
    canon16, key16 = await _resolve(
        tmp_path, FLASH + "_clean_tempo_v2", {"speed": 1.16}, 16000
    )
    canon8, key8 = await _resolve(
        tmp_path, FLASH + "_clean_tempo_v2", {"speed": 1.16}, 8000
    )
    assert key16 != key8, "stores the finished audio per format"
    assert "speed" in canon16 and "tempo" not in canon16, "speed stays native"


async def test_bare_flash_key_is_format_agnostic_as_before(tmp_path):
    _c, key16 = await _resolve(tmp_path, FLASH, {"speed": 1.16}, 16000)
    _c, key8 = await _resolve(tmp_path, FLASH, {"speed": 1.16}, 8000)
    assert key16 == key8


async def test_v3_still_turns_speed_into_tempo(tmp_path):
    canon, _key = await _resolve(
        tmp_path, "eleven_v3_conversational_clean_tempo_v2", {"speed": 1.16}, 16000
    )
    assert "tempo" in canon and "speed" not in canon
