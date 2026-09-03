"""ElevenLabs v3 (Text-to-Dialogue) support tests.

Covers the wire differences the v3 socket introduces versus the v2
text-to-speech multi-context socket:

- connect URI: ``/v1/text-to-dialogue/multi-stream-input``, no voice in the
  path, ``sync_alignment=true``, no ``auto_mode``/``inactivity_timeout``;
- per-connection keepalive context (TTD auto-closes after 20s of client
  silence, and a keepalive must name a registered context);
- utterance framing: voices-registration init + ``inputs`` entry with
  ``new_turn`` (not the v2 bare-space init + ``text``);
- ``is_final_audio_for_turn`` as an end-of-utterance marker;
- provider-level shaping: voice_settings filtered to ``stability`` only,
  SSML parsing refused for v3, language_code sent, pool key keeps language.

The pool logic runs against a fake socket (no network). The live wire schema
was smoke-tested against the real Text-to-Dialogue endpoint with the
India-residency key (see smoke/e2e_smoke.py for the manual harness).
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from app.providers.elevenlabs import ElevenLabsProvider
from app.providers.elevenlabs_pool import (
    _KEEPALIVE_CONTEXT_ID,
    ElevenLabsStreamPool,
)

VOICE = "fG9s0SXJb213f4UxVHyG"
BASE = "https://api.in.residency.elevenlabs.io"
V3_MODEL = "eleven_v3_conversational"


class FakeSocket:
    """Minimal websockets-like object: send() records, async-for yields fed
    messages. ``feed(None)`` ends the iteration (server closed)."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    def feed(self, obj: Any) -> None:
        self._incoming.put_nowait(json.dumps(obj) if obj is not None else None)

    async def close(self) -> None:
        self.closed = True
        self.feed(None)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        value = await self._incoming.get()
        if value is None:
            raise StopAsyncIteration
        return value


class FakeConnect:
    """ConnectFn double: hands out FakeSockets and records every URI."""

    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.uris: list[str] = []

    def __call__(self, uri: str, headers: dict):
        self.uris.append(uri)
        socket = FakeSocket()
        self.sockets.append(socket)

        class _Ctx:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                return False

        return _Ctx()


async def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        try:
            if predicate():
                return
        except (IndexError, AttributeError, StopIteration):
            # Connection task / sent messages not populated yet.
            pass
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


async def _run_stream(pool: ElevenLabsStreamPool, msg: dict) -> list[bytes]:
    """Drive pool.stream() to completion: start it, wait for the flush to be
    sent, then answer with one audio chunk + the turn-end marker."""
    await pool._conns[0].ready.wait()
    socket = pool._conns[0].ws
    collected: list[bytes] = []

    async def consume():
        async for chunk in pool.stream(msg):
            collected.append(chunk)

    task = asyncio.create_task(consume())
    await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
    ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
    socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"abcd").decode()})
    socket.feed({"context_id": ctx_id, "is_final_audio_for_turn": True})
    await asyncio.wait_for(task, timeout=2.0)
    return collected


def _pool(model_id: str, connect: FakeConnect, **kwargs) -> ElevenLabsStreamPool:
    return ElevenLabsStreamPool(
        api_key="test-key",
        voice_id=VOICE,
        model_id=model_id,
        base_url=BASE,
        connect_fn=connect,
        min_size=1,
        max_size=1,
        language=kwargs.pop("language", None),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# URI construction
# ---------------------------------------------------------------------------


def test_v3_uri_targets_text_to_dialogue():
    connect = FakeConnect()
    pool = _pool(V3_MODEL, connect, language="en")
    assert "/v1/text-to-dialogue/multi-stream-input" in pool._uri
    assert f"model_id={V3_MODEL}" in pool._uri
    assert "output_format=pcm_16000" in pool._uri
    assert "sync_alignment=true" in pool._uri
    # v3 registers the voice per context — not in the URL.
    assert VOICE not in pool._uri
    # TTD params only: the v2-only knobs must be absent.
    assert "auto_mode" not in pool._uri
    assert "inactivity_timeout" not in pool._uri
    # v3 accepts a connect-time language_code.
    assert "language_code=en" in pool._uri


def test_v2_uri_unchanged():
    connect = FakeConnect()
    pool = _pool("eleven_flash_v2_5", connect, language="en")
    assert "/v1/text-to-speech/{v}/multi-stream-input".format(v=VOICE) in pool._uri
    assert "auto_mode=true" in pool._uri
    assert "inactivity_timeout=120" in pool._uri
    assert "language_code=en" in pool._uri  # flash is multilingual

    pool = _pool("eleven_multilingual_v2", connect, language="en")
    assert "language_code" not in pool._uri  # non-multilingual: omitted


# ---------------------------------------------------------------------------
# Wire protocol
# ---------------------------------------------------------------------------


async def test_v3_registers_keepalive_context_on_connect():
    connect = FakeConnect()
    pool = _pool(V3_MODEL, connect)
    await pool.start()
    try:
        await _wait_for(lambda: len(connect.sockets[0].sent) >= 1)
        first = connect.sockets[0].sent[0]
        assert first == {"context_id": _KEEPALIVE_CONTEXT_ID, "voices": [VOICE]}
    finally:
        await pool.aclose()


async def test_v3_keepalive_pings_registered_context():
    connect = FakeConnect()
    pool = _pool(V3_MODEL, connect, keepalive_interval=0.05)
    await pool.start()
    try:
        await pool._conns[0].ready.wait()
        socket = pool._conns[0].ws

        async def got_ping():
            return any(
                m.get("keep_alive") is True
                and m.get("context_id") == _KEEPALIVE_CONTEXT_ID
                for m in socket.sent
            )

        await _wait_for(got_ping)
    finally:
        await pool.aclose()


async def test_context_slot_cap_accounts_for_keepalive():
    """TTD allows 5 contexts per connection and the keepalive context occupies
    one of them (probe-confirmed: server rejects a 5th USER context with
    ``too_many_contexts``), so a v3 connection exposes 4 usable slots while a
    v2 connection keeps all 5."""
    v3_connect = FakeConnect()
    v3_pool = _pool(V3_MODEL, v3_connect)
    v2_connect = FakeConnect()
    v2_pool = _pool("eleven_flash_v2_5", v2_connect)
    await v3_pool.start()
    await v2_pool.start()
    try:
        await v3_pool._conns[0].ready.wait()
        await v2_pool._conns[0].ready.wait()
        assert v3_pool._conns[0].max_contexts == 4
        assert v2_pool._conns[0].max_contexts == 5
        # A v3 socket at 4 in-flight streams must not be offered by
        # _available() (the 5th would be rejected server-side).
        v3_pool._conns[0].inflight = 4
        assert v3_pool._available() == []
        v3_pool._conns[0].inflight = 3
        assert v3_pool._available() == [v3_pool._conns[0]]
    finally:
        v3_pool._conns[0].inflight = 0
        await v3_pool.aclose()
        await v2_pool.aclose()


async def test_v2_socket_sends_no_keepalive():
    connect = FakeConnect()
    pool = _pool("eleven_flash_v2_5", connect, keepalive_interval=0.05)
    await pool.start()
    try:
        await asyncio.sleep(0.2)
        assert connect.sockets[0].sent == []  # v2 relies on inactivity_timeout
    finally:
        await pool.aclose()


async def test_v3_stream_protocol():
    connect = FakeConnect()
    pool = _pool(V3_MODEL, connect)
    await pool.start()
    try:
        chunks = await _run_stream(
            pool, {"text": "hello there", "voice_settings": {"stability": 0.4}}
        )
        assert chunks == [b"abcd"]

        sent = [
            m
            for m in connect.sockets[0].sent
            if m.get("context_id") != _KEEPALIVE_CONTEXT_ID
        ]
        ctx_id = next(m["context_id"] for m in sent if m.get("flush"))
        # 1) context opens with a voices registration (+ stability only)
        init = sent[0]
        assert init["context_id"] == ctx_id
        assert init["voices"] == [VOICE]
        assert init["voice_settings"] == {"stability": 0.4}
        # 2) the utterance is ONE input starting a new turn
        assert sent[1] == {
            "context_id": ctx_id,
            "inputs": [{"text": "hello there", "voice_id": VOICE, "new_turn": True}],
        }
        # 3) flush triggers generation
        assert sent[2] == {"context_id": ctx_id, "flush": True}
        # 4) close_context frees the server-side context, socket stays warm
        assert sent[3] == {"context_id": ctx_id, "close_context": True}
    finally:
        await pool.aclose()


async def test_v2_stream_protocol_unchanged():
    connect = FakeConnect()
    pool = _pool("eleven_flash_v2_5", connect)
    await pool.start()
    try:
        await pool._conns[0].ready.wait()
        socket = pool._conns[0].ws
        collected: list[bytes] = []

        async def consume():
            async for chunk in pool.stream(
                {"text": "hello there", "voice_settings": {"speed": 1.0}}
            ):
                collected.append(chunk)

        task = asyncio.create_task(consume())
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"xy").decode()})
        socket.feed({"context_id": ctx_id, "is_final": True})
        await asyncio.wait_for(task, timeout=2.0)
        assert collected == [b"xy"]

        sent = [m for m in socket.sent]
        assert sent[0] == {
            "text": " ",
            "context_id": ctx_id,
            "voice_settings": {"speed": 1.0},
        }
        assert sent[1] == {"text": "hello there", "context_id": ctx_id}
        assert sent[2] == {"context_id": ctx_id, "flush": True}
        assert sent[3] == {"context_id": ctx_id, "close_context": True}
    finally:
        await pool.aclose()


async def test_v3_idle_gap_ends_stream():
    """Without is_final markers, the short idle gap after audio ends the
    stream (ElevenLabs parks the context otherwise)."""
    connect = FakeConnect()
    pool = _pool(V3_MODEL, connect, idle_timeout=0.05)
    await pool.start()
    try:
        await pool._conns[0].ready.wait()
        socket = pool._conns[0].ws
        collected: list[bytes] = []

        async def consume():
            async for chunk in pool.stream({"text": "hello there"}):
                collected.append(chunk)

        task = asyncio.create_task(consume())
        await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
        ctx_id = next(m["context_id"] for m in socket.sent if m.get("flush"))
        socket.feed({"context_id": ctx_id, "audio": base64.b64encode(b"zz").decode()})
        await asyncio.wait_for(task, timeout=2.0)
        assert collected == [b"zz"]
    finally:
        await pool.aclose()


# ---------------------------------------------------------------------------
# Provider shaping
# ---------------------------------------------------------------------------


def test_voice_settings_v3_keeps_stability_only():
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    vs = provider._voice_settings(
        {"speed": 1.2, "stability": 0.4, "similarity_boost": 0.8}, V3_MODEL
    )
    assert vs == {"stability": 0.4}
    # No stability configured -> nothing is sent (TTD defaults apply).
    assert provider._voice_settings({"speed": 1.2}, V3_MODEL) == {}


def test_voice_settings_v2_unchanged():
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    vs = provider._voice_settings(
        {"speed": 1.2, "stability": 0.4, "similarity_boost": 0.8},
        "eleven_flash_v2_5",
    )
    assert vs == {"speed": 1.2, "stability": 0.4, "similarity_boost": 0.8}


def test_get_pool_language_keying():
    provider = ElevenLabsProvider(api_key="k", base_url=BASE)
    provider._get_pool(VOICE, "eleven_multilingual_v2", language="en")
    provider._get_pool(VOICE, V3_MODEL, language="en")
    keys = sorted(provider._pools.keys())
    # Non-multilingual v2 model drops language from the key; v3 keeps it.
    assert keys == [
        (VOICE, "eleven_multilingual_v2", False, None),
        (VOICE, V3_MODEL, False, "en"),
    ]


@pytest.mark.parametrize(
    "model,expect_ssml",
    [("eleven_flash_v2_5", True), (V3_MODEL, False)],
)
async def test_synth_http_shaping(model, expect_ssml):
    import respx
    from httpx import Response

    provider = ElevenLabsProvider(api_key="k", base_url=BASE)

    with respx.mock:
        route = respx.post(f"{BASE}/v1/text-to-speech/{VOICE}").mock(
            return_value=Response(200, content=b"pcm-bytes")
        )
        result = await provider.synth(
            text="hi",
            voice_id=VOICE,
            model=model,
            language="en-IN",
            params={"enable_ssml_parsing": True, "speed": 1.1, "stability": 0.5},
        )

    assert result.audio == b"pcm-bytes"
    request = route.calls.last.request
    assert "output_format=pcm_16000" in str(request.url)
    payload = json.loads(request.content)
    assert payload["model_id"] == model
    assert payload["language_code"] == "en"
    if expect_ssml:
        assert payload["enable_ssml_parsing"] is True
        assert payload["voice_settings"] == {"speed": 1.1, "stability": 0.5}
    else:
        assert "enable_ssml_parsing" not in payload
        assert payload["voice_settings"] == {"stability": 0.5}
    await provider.aclose()
