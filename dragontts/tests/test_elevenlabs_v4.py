"""Eleven v4 (eleven_v4, eleven_v4_turbo) rides the Text-to-Dialogue path.

ElevenLabs serves v4 ONLY on the Text-to-Dialogue socket — the classic
text-to-speech multi-context socket rejects it (HTTP 400, verified live on the
India residency host), as it does v3. Routing keyed on the "eleven_v3" prefix
alone sent v4 to that classic socket, so every v4 request failed. The contract:

- eleven_v3* and eleven_v4* are Text-to-Dialogue models; flash / turbo_v2_5 /
  multilingual_v2 are not;
- a v4 request speaks the TTD wire sequence (voices registration, ``inputs``
  with new_turn, flush) on the TTD socket, never the classic socket or HTTP;
- v4 gets the TTD pool conventions (dialogue pool size, connect-time
  language_code, stability-only voice_settings, no SSML, no leading dot);
- v4 audio is served as generated — none of the v3-tuned hygiene;
- the v4 base generates at ELEVENLABS_V4_NATIVE_SAMPLE_RATE (8 kHz default,
  like v3) and a /tts/stream miss requested at that rate streams LIVE;
- v4 takes the same local pipeline suffixes as v3 conversational
  (_tempo / _clean_tempo / _clean_tempo_v2) with the same chains.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app.audio.text import prepend_leading_dot
from app.cache.service import CacheService
from app.core.config import settings
from app.providers import elevenlabs, elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import (
    ElevenLabsStreamPool,
    has_local_pipeline,
    is_elevenlabs_ttd_model,
    is_elevenlabs_v4_model,
    normalize_pipeline_model,
    pipeline_family,
    pipeline_variant,
)
from app.schemas.tts import CartesiaVoice, OutputFormat, TTSRequest
from app.storage.filesystem import FilesystemBlobStore
from app.storage.sqlite import SQLiteMetadataStore
from tests.test_atempo import _resolve_key
from tests.test_elevenlabs_v3 import BASE, VOICE, FakeConnect, _wait_for

V4_TURBO = "eleven_v4_turbo"


@pytest.fixture(autouse=True)
def _pin_native_rate(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 8000)
    monkeypatch.setattr(settings, "elevenlabs_v4_native_sample_rate", 8000)


class _NoHTTPClient:
    async def post(self, *args, **kwargs):
        raise AssertionError("v4 must not hit the HTTP text-to-speech endpoint")

    async def aclose(self):
        return None


@pytest.fixture
def connect(monkeypatch) -> FakeConnect:
    fake = FakeConnect()

    def _fake_connect(uri, additional_headers=None, open_timeout=None):
        return fake(uri, additional_headers or {})

    monkeypatch.setattr(elevenlabs_pool, "connect", _fake_connect)
    return fake


@pytest.fixture
def provider(connect) -> ElevenLabsProvider:
    p = ElevenLabsProvider(api_key="k", base_url=BASE)
    p._client = _NoHTTPClient()
    return p


@pytest.mark.parametrize(
    "model, ttd, v4",
    [
        ("eleven_v4_turbo", True, True),
        ("eleven_v4", True, True),
        ("eleven_v3", True, False),
        ("eleven_v3_conversational", True, False),
        ("eleven_v3_conversational_clean_tempo_v2", True, False),
        ("eleven_flash_v2_5", False, False),
        ("eleven_turbo_v2_5", False, False),
        ("eleven_multilingual_v2", False, False),
        (None, False, False),
        ("", False, False),
    ],
)
def test_model_family(model, ttd, v4):
    assert is_elevenlabs_ttd_model(model) is ttd
    assert is_elevenlabs_v4_model(model) is v4


def test_v4_pool_uses_the_text_to_dialogue_socket():
    pool = ElevenLabsStreamPool(
        api_key="k",
        voice_id=VOICE,
        model_id=V4_TURBO,
        base_url=BASE,
        connect_fn=FakeConnect(),
        language="hi",
        output_format="pcm_8000",
    )
    assert pool._uri.startswith(
        "wss://api.in.residency.elevenlabs.io/v1/text-to-dialogue/multi-stream-input?"
    )
    assert f"model_id={V4_TURBO}" in pool._uri
    assert "output_format=pcm_8000" in pool._uri
    assert "language_code=hi" in pool._uri
    assert "auto_mode" not in pool._uri  # classic-socket-only param


def test_v4_pool_conventions(provider, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", 3)
    pool = provider._get_pool(VOICE, V4_TURBO, False, "hi", 8000)
    assert pool is not None
    assert pool._min_size == 3, "sized by the dialogue knob, not the classic one"
    assert "language_code=hi" in pool._uri, "TTD keeps the connect-time language"
    assert "output_format=pcm_8000" in pool._uri
    assert provider.synth_native_format(V4_TURBO) == ("pcm_s16le", 8000)


def test_v4_voice_settings_keep_only_stability(provider):
    vs = provider._voice_settings(
        {"speed": 0.8, "stability": 0.4, "similarity_boost": 0.7}, V4_TURBO
    )
    assert vs == {"stability": 0.4}


def test_v4_gets_no_leading_dot(monkeypatch):
    monkeypatch.setattr(settings, "tts_leading_dot", True)
    assert prepend_leading_dot("599 rupees", "elevenlabs", V4_TURBO) == "599 rupees"
    assert (
        prepend_leading_dot("599 rupees", "elevenlabs", "eleven_flash_v2_5")
        == ".599 rupees"
    )


async def _answer(connect: FakeConnect, audio: bytes) -> dict:
    """Wait for the utterance flush on the (only) socket, answer it, and
    return the utterance messages the pool sent."""
    await _wait_for(lambda: len(connect.sockets) == 1)
    socket = connect.sockets[0]
    await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
    ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
    socket.feed({"context_id": ctx_id, "audio": base64.b64encode(audio).decode()})
    socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})
    return {
        "ctx_id": ctx_id,
        "sent": [m for m in socket.sent if m.get("context_id") == ctx_id],
    }


@pytest.mark.parametrize("native", [8000, 16000])
async def test_v4_base_audio_is_labelled_with_the_rate_it_was_made_at(
    provider, connect, monkeypatch, native
):
    """The cache converts the native clip to the caller's format from this
    label; a hard-coded 8000 made a 16 kHz v4 clip play at half speed."""
    monkeypatch.setattr(settings, "elevenlabs_v4_native_sample_rate", native)
    try:
        task = asyncio.create_task(
            provider.synth(
                text="hello there",
                voice_id=VOICE,
                model=V4_TURBO,
                language="hi-IN",
                params={},
            )
        )
        await _answer(connect, b"\x01\x02" * 400)
        result = await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()
    assert f"output_format=pcm_{native}" in connect.uris[0]
    assert result.sample_rate == native


async def test_v4_synth_speaks_ttd_and_serves_audio_unaltered(
    provider, connect, monkeypatch
):
    def _no_hygiene(*args, **kwargs):
        raise AssertionError("v3-tuned hygiene must not touch v4 audio")

    monkeypatch.setattr(elevenlabs, "clean_utterance", _no_hygiene)
    # Leading/trailing silence that v3 hygiene WOULD trim.
    audio = b"\x00\x00" * 800 + b"\x10\x27" * 800 + b"\x00\x00" * 800
    try:
        task = asyncio.create_task(
            provider.synth(
                text="नमस्ते, मैं Flipkart से प्रियंका बोल रही हूँ.",
                voice_id=VOICE,
                model=V4_TURBO,
                language="hi-IN",
                params={"speed": 0.8, "stability": 0.4, "enable_ssml_parsing": True},
            )
        )
        wire = await _answer(connect, audio)
        result = await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()

    assert "/v1/text-to-dialogue/multi-stream-input" in connect.uris[0]
    assert f"model_id={V4_TURBO}" in connect.uris[0]
    assert "language_code=hi" in connect.uris[0]
    ctx_id = wire["ctx_id"]
    assert wire["sent"][:3] == [
        {"context_id": ctx_id, "voices": [VOICE], "voice_settings": {"stability": 0.4}},
        {
            "context_id": ctx_id,
            "inputs": [
                {
                    "text": "नमस्ते, मैं Flipkart से प्रियंका बोल रही हूँ.",
                    "voice_id": VOICE,
                    "new_turn": True,
                }
            ],
        },
        {"context_id": ctx_id, "flush": True},
    ]
    assert result.audio == audio, "v4 audio is served exactly as generated"
    assert result.sample_rate == 8000
    assert "output_format=pcm_8000" in connect.uris[0]


async def test_v4_stream_synth_uses_the_ttd_pool(provider, connect):
    collected: list[bytes] = []

    async def consume():
        async for chunk in provider.stream_synth(
            text="hello there",
            voice_id=VOICE,
            model=V4_TURBO,
            language="en",
            params={"enable_ssml_parsing": True},
        ):
            collected.append(chunk)

    try:
        task = asyncio.create_task(consume())
        await _answer(connect, b"abcd")
        await asyncio.wait_for(task, timeout=3.0)
        (key,) = provider._pools
    finally:
        await provider.aclose()

    assert collected == [b"abcd"]
    assert "/v1/text-to-dialogue/multi-stream-input" in connect.uris[0]
    assert "enable_ssml_parsing" not in connect.uris[0]
    assert key[1] == V4_TURBO and key[2] is False, "SSML normalized out of the key"


async def test_v4_stream_miss_streams_live_at_its_native_rate(
    provider, connect, monkeypatch, tmp_path
):
    """The first chunk reaches the caller BEFORE the utterance finishes.

    If v4's native rate differed from the request, the cache would synthesize
    the whole sentence first and this first read would block until the end
    marker (which is only fed after it).
    """
    monkeypatch.setattr(settings, "metrics_write_behind_enabled", False)
    meta = SQLiteMetadataStore(str(tmp_path / "cache.db"))
    await meta.init()
    blobs = FilesystemBlobStore(str(tmp_path / "blobs"))
    await blobs.init()
    svc = CacheService(meta, blobs, lambda n: provider if n == "elevenlabs" else None)
    req = TTSRequest(
        model_id=f"elevenlabs:{V4_TURBO}",
        transcript="आपका order कल तक deliver हो जाएगा.",
        voice=CartesiaVoice(id=VOICE),
        language="hi",
        output_format=OutputFormat(encoding="pcm_s16le", sample_rate=8000),
    )
    try:
        headers, body = await svc.stream(req)
        assert headers["X-Cache"] == "MISS"
        first = asyncio.ensure_future(body.__anext__())
        await _wait_for(lambda: len(connect.sockets) == 1)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"ab").decode()})
        assert await asyncio.wait_for(first, timeout=1.0) == b"ab", "streamed live"
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"cd").decode()})
        socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})
        rest = [chunk async for chunk in body]
    finally:
        await provider.aclose()
    assert rest == [b"cd"]
    assert "output_format=pcm_8000" in connect.uris[0]


# ---------------------------------------------------------------------------
# v4 carries the same local pipeline variants as v3 conversational
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model, family, variant",
    [
        ("eleven_v4_turbo", "eleven_v4_turbo", "base"),
        ("eleven_v4_turbo_tempo", "eleven_v4_turbo", "tempo"),
        ("eleven_v4_turbo_clean_tempo", "eleven_v4_turbo", "clean_tempo"),
        ("eleven_v4_turbo_clean_tempo_v2", "eleven_v4_turbo", "clean_tempo_v2"),
        ("eleven_v4", "eleven_v4", "base"),
        ("eleven_v4_clean_tempo", "eleven_v4", "clean_tempo"),
        (
            "eleven_v3_conversational_clean_tempo_v2",
            "eleven_v3_conversational",
            "clean_tempo_v2",
        ),
        # v3 conversational keeps its legacy prefix match...
        ("eleven_v3_conversational_other", "eleven_v3_conversational", "base"),
        # ...but a future eleven_v4_* model is never read as a v4 variant.
        ("eleven_v4_flash", None, None),
        ("eleven_v3", None, None),
        ("eleven_flash_v2_5", None, None),
    ],
)
def test_pipeline_family_and_variant(model, family, variant):
    assert pipeline_family(model) == family
    assert pipeline_variant(model) == variant
    assert has_local_pipeline(model) is (family is not None)
    assert normalize_pipeline_model(model) == (family or model)


def test_v4_base_speaks_its_own_rate_variants_the_full_band_rate(provider, monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 44100)
    assert provider.synth_native_format(V4_TURBO) == ("pcm_s16le", 8000)
    for suffix in ("_tempo", "_clean_tempo", "_clean_tempo_v2"):
        assert provider.synth_native_format(V4_TURBO + suffix) == ("pcm_s16le", 44100)


def test_v4_tempo_is_honored_but_not_the_v3conv_default(provider, monkeypatch):
    monkeypatch.setattr(elevenlabs, "atempo_available", lambda: True)
    monkeypatch.setattr(settings, "elevenlabs_atempo_enabled", True)
    monkeypatch.setattr(settings, "elevenlabs_v3conv_default_tempo", 1.15)
    assert provider._tempo_for({"tempo": 1.2}, V4_TURBO) == 1.2
    assert provider._tempo_for({"tempo": 1.2}, V4_TURBO + "_clean_tempo") == 1.2
    # The global v3-conversational speed-up knob must not leak onto v4.
    assert provider._tempo_for({}, V4_TURBO) == 1.0
    assert provider._tempo_for({}, "eleven_v3_conversational") == 1.15
    assert provider._tempo_for({"tempo": 1.2}, "eleven_v3") == 1.0


def test_v4_cache_maps_speed_to_tempo_and_keys_the_output_format():
    key_speed, params_speed = _resolve_key(V4_TURBO, {"speed": 1.15})
    key_tempo, _ = _resolve_key(V4_TURBO, {"tempo": 1.15})
    assert key_speed == key_tempo and params_speed == {"tempo": 1.15}
    # Stored bytes are the final result per format, as for v3 conversational.
    assert (
        _resolve_key(V4_TURBO, {}, encoding="pcm_mulaw", sample_rate=8000)[0]
        != _resolve_key(V4_TURBO, {}, encoding="pcm_s16le", sample_rate=8000)[0]
    )


@pytest.mark.parametrize(
    "suffix, cleaned, released, joined",
    [
        ("", False, False, False),
        ("_tempo", False, False, False),
        ("_clean_tempo", True, True, False),
        ("_clean_tempo_v2", True, True, True),
    ],
)
async def test_v4_variant_runs_the_v3_chain(
    provider, connect, monkeypatch, suffix, cleaned, released, joined
):
    calls: list[str] = []

    def _record(name):
        def _stage(audio, *args, **kwargs):
            calls.append(name)
            return audio

        return _stage

    monkeypatch.setattr(settings, "elevenlabs_utterance_hygiene_enabled", True)
    monkeypatch.setattr(elevenlabs, "clean_utterance", _record("hygiene"))
    monkeypatch.setattr(elevenlabs, "soften_end", _record("end_release"))
    monkeypatch.setattr(
        ElevenLabsProvider, "_v2_join_chain", staticmethod(_record("join"))
    )
    audio = b"\x10\x27" * 1600
    try:
        task = asyncio.create_task(
            provider.synth(
                text="आपका order कल तक deliver हो जाएगा.",
                voice_id=VOICE,
                model=V4_TURBO + suffix,
                language="hi",
                params={},
            )
        )
        await _answer(connect, audio)
        result = await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()

    # ElevenLabs only ever sees the base id; the suffix is DragonTTS-local.
    assert f"model_id={V4_TURBO}&" in connect.uris[0]
    assert ("hygiene" in calls) is cleaned
    assert ("end_release" in calls) is released
    assert ("join" in calls) is joined
    assert result.audio == audio  # stages are identity here: nothing else ran
