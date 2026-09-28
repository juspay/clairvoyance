"""Startup warm pool (ELEVENLABS_WARM_*).

A classic socket pins voice + model + language when it connects, so the
sockets opened at startup only help if they are the exact pool real misses
land in. These tests pin that: the default warm target is the production flash
template (iB2rIwm9cQCRGWoKDRtX, eleven_flash_v2_5, language "en" — a null
template language reaches dragontts as clairvoyance's default "en"), a miss
from that template reuses the warm sockets without opening a new one, and the
warm target stays separate from PROVIDER_DEFAULTS (which key the cache).
"""

from __future__ import annotations

import asyncio
import base64

from app.core.config import PROVIDER_DEFAULTS, settings
from app.providers import elevenlabs_pool
from app.providers.elevenlabs import ElevenLabsProvider
from tests.test_elevenlabs_v3 import BASE, FakeConnect, _wait_for

WARM_VOICE = "iB2rIwm9cQCRGWoKDRtX"
FLASH = "eleven_flash_v2_5"


def _install_fake_connect(monkeypatch, connect: FakeConnect) -> None:
    """Route the provider's real websockets.connect() calls to the fake."""

    def _fake_connect(uri, additional_headers=None, open_timeout=None):
        return connect(uri, additional_headers or {})

    monkeypatch.setattr(elevenlabs_pool, "connect", _fake_connect)


def test_warm_target_defaults_to_the_production_flash_template():
    assert settings.elevenlabs_warm_voice_id == WARM_VOICE
    assert settings.elevenlabs_warm_model == FLASH
    assert settings.elevenlabs_warm_language == "en"
    # Cache keys collapse params equal to PROVIDER_DEFAULTS, so those must NOT
    # move with the warm target (a default speed of 0.8 would make an explicit
    # speed 0.8 share a key with audio synthesized at 1.0).
    assert PROVIDER_DEFAULTS["elevenlabs"]["speed"] == 1.0
    assert PROVIDER_DEFAULTS["elevenlabs"]["voice_id"] == "fG9s0SXJb213f4UxVHyG"


async def test_template_miss_reuses_the_warm_sockets(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_stream_pool_size", 3)
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        await provider.warm()
        await _wait_for(lambda: len(connect.sockets) == 3)
        assert all(f"/text-to-speech/{WARM_VOICE}/" in u for u in connect.uris)
        assert all("model_id=eleven_flash_v2_5" in u for u in connect.uris)
        assert all("language_code=en" in u for u in connect.uris)
        (pool,) = provider._pools.values()
        await _wait_for(lambda: all(c.ready.is_set() for c in pool._conns))

        # Exactly what clairvoyance sends for this template: speed rides the
        # sentence; the null language arrives as "en".
        collected: list[bytes] = []

        async def consume():
            async for chunk in provider.stream_synth(
                text="hello",
                voice_id=WARM_VOICE,
                model=FLASH,
                language="en",
                params={"speed": 0.8},
            ):
                collected.append(chunk)

        task = asyncio.create_task(consume())

        def _flushed():
            for socket in connect.sockets:
                for m in socket.sent:
                    if m.get("flush"):
                        return socket, m["context_id"]
            return None

        await _wait_for(lambda: _flushed() is not None)
        flushed = _flushed()
        assert flushed is not None
        socket, ctx_id = flushed
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"ab").decode()})
        socket.feed({"context_id": ctx_id, "isFinal": True})
        await asyncio.wait_for(task, timeout=2.0)

        assert collected == [b"ab"]
        assert len(connect.sockets) == 3, "the miss must reuse a warm socket"
        assert len(provider._pools) == 1, "the miss must land in the warm pool"
    finally:
        await provider.aclose()


async def test_empty_warm_voice_opens_nothing(monkeypatch):
    monkeypatch.setattr(settings, "elevenlabs_warm_voice_id", "")
    connect = FakeConnect()
    _install_fake_connect(monkeypatch, connect)
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    try:
        await provider.warm()
        await asyncio.sleep(0.05)
        assert connect.sockets == []
        assert provider._pools == {}
    finally:
        await provider.aclose()
