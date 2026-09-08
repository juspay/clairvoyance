"""ElevenLabs v3 bytes-path routing + TTD pool sizing.

The /tts/bytes path (get_or_synthesize → provider.synth) previously fell
through to the classic HTTP endpoint for eleven_v3, which 404s — v3 exists
only on the Text-to-Dialogue socket. These tests pin the new behavior:
synth() speaks one TTD utterance over the warm pool, the TTD pool defaults to
TWO warm sockets (its own knob, independent of the classic 16), and a
disabled TTD pool fails loudly instead of falling back to HTTP.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.base import ProviderError
from app.audio.text import prepend_leading_dot

from tests.test_elevenlabs_v3 import BASE, VOICE, FakeConnect, V3_MODEL


class _NoHTTPClient:
    """Fails any HTTP attempt — v3 must never reach the classic endpoint."""

    async def post(self, *args, **kwargs):
        raise AssertionError("eleven_v3 must not hit the HTTP text-to-speech endpoint")

    async def aclose(self):
        return None


def _provider(connect: FakeConnect) -> ElevenLabsProvider:
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._client = _NoHTTPClient()
    return provider


def _install_fake_connect(monkeypatch, connect: FakeConnect) -> None:
    """Route real websockets.connect() calls to the fake."""

    def _fake_connect(uri, additional_headers=None, open_timeout=None):
        return connect(uri, additional_headers or {})

    monkeypatch.setattr(elevenlabs_pool, "connect", _fake_connect)


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


async def test_synth_v3_speaks_over_ttd_pool_not_http(monkeypatch):
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = _provider(connect)
    try:
        synth = asyncio.create_task(
            provider.synth(
                text="hello there",
                voice_id=VOICE,
                model=V3_MODEL,
                language="en-IN",
                params={"stability": 0.4},
            )
        )
        # The pool connects lazily on first stream — wait for the socket.
        await _wait_for(lambda: connect.sockets)
        socket = connect.sockets[0]
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed(
            {"context_id": ctx_id, "audio": base64.b64encode(b"abcd").decode()}
        )
        socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})
        result = await asyncio.wait_for(synth, timeout=2.0)

        assert result.audio == b"abcd"
        # v3 synthesizes NATIVELY at 8 kHz (TTD output_format=pcm_8000) —
        # telephony requests are served without any resampling.
        assert result.encoding == "pcm_s16le"
        assert result.sample_rate == 8000
        assert "output_format=pcm_8000" in connect.uris[0]
        assert provider.synth_native_format(V3_MODEL) == ("pcm_s16le", 8000)
        assert provider.synth_native_format("eleven_flash_v2_5") == ("pcm_s16le", 16000)
        # The utterance went out as ONE TTD input with the stability-only
        # voice settings on the context registration.
        stream_msgs = [
            m for m in socket.sent if m.get("context_id") != "dragontts-keepalive"
        ]
        inputs = next(m for m in stream_msgs if "inputs" in m)
        assert inputs["inputs"][0]["text"] == "hello there"
        assert inputs["inputs"][0]["voice_id"] == VOICE
        assert inputs["inputs"][0]["new_turn"] is True
        init = next(m for m in stream_msgs if "voices" in m)
        assert init["voice_settings"] == {"stability": 0.4}
    finally:
        await provider.aclose()


async def test_v3_pool_defaults_to_two_sockets(monkeypatch):
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = _provider(connect)
    try:
        v3_pool = provider._get_pool(VOICE, V3_MODEL, False, "en")
        assert v3_pool._min_size == settings.elevenlabs_dialogue_pool_size == 2
        assert v3_pool._max_size == 6
        assert "output_format=pcm_8000" in v3_pool._uri
        # Classic models keep the classic knob AND the 16 kHz socket format.
        v2_pool = provider._get_pool(VOICE, "eleven_flash_v2_5", False, None)
        assert v2_pool._min_size == settings.elevenlabs_stream_pool_size
        assert "output_format=pcm_16000" in v2_pool._uri
        assert v3_pool is not v2_pool
    finally:
        await provider.aclose()


async def test_synth_v3_fails_loudly_when_pool_disabled(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_dialogue_pool_size", 0)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = _provider(connect)
    try:
        with pytest.raises(ProviderError):
            await provider.synth(
                text="hello", voice_id=VOICE, model=V3_MODEL, language=None, params={}
            )
    finally:
        await provider.aclose()


def test_leading_dot_skipped_for_v3():
    assert prepend_leading_dot("hello", "elevenlabs", "eleven_flash_v2_5") == ".hello"
    # Text-to-Dialogue speaks the text verbatim (matches the verified direct
    # pipecat path) — no leading-dot hint.
    assert prepend_leading_dot("hello", "elevenlabs", V3_MODEL) == "hello"
