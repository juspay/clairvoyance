"""v3 (Text-to-Dialogue) synth retries ONCE when an attempt fails.

Prod failure this pins down: the network drops a TTD socket ("no close frame
received or sent"), every utterance on it fails, and — with no retry on the
v3 path — the sentence is silently missing from the live call. The contract:

- a failed attempt is retried once, on another socket, and the caller gets
  ONLY the retry's audio (the failed attempt's partial audio is discarded, so
  nothing is ever duplicated);
- if the retry also fails the error is raised as before and logged with the
  full text;
- cancellation (caller hung up / barge-in) is never retried;
- a first-try success does not retry or warn.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from loguru import logger
from websockets.exceptions import ConnectionClosedError

from app.core.config import settings
from app.providers import elevenlabs_pool
from app.providers.base import ProviderError
from app.providers.elevenlabs import ElevenLabsProvider
from tests.test_elevenlabs_v3 import BASE, V3_MODEL, VOICE, FakeSocket, _wait_for

TEXT = "आपकी thirty two thousand eight hundred and forty four rupees तक की credit line ready है."
_DROP = "__network_drop__"


class DroppableSocket(FakeSocket):
    """FakeSocket that can die the way prod sockets do: the connection ends
    with no close frame, so iteration raises ConnectionClosedError."""

    def drop(self) -> None:
        self._incoming.put_nowait(_DROP)

    async def __anext__(self) -> str:
        value = await self._incoming.get()
        if value == _DROP:
            raise ConnectionClosedError(None, None)
        if value is None:
            raise StopAsyncIteration
        return value


class DroppableConnect:
    def __init__(self) -> None:
        self.sockets: list[DroppableSocket] = []

    def __call__(self, uri: str, headers: dict):
        socket = DroppableSocket()
        self.sockets.append(socket)

        class _Ctx:
            async def __aenter__(self):
                return socket

            async def __aexit__(self, *args):
                return False

        return _Ctx()


@pytest.fixture(autouse=True)
def _pin_v3_native_rate(monkeypatch):
    # Base model + 8 kHz + tempo 1 = the direct path: audio returned exactly
    # as generated, so the tests can assert on the bytes.
    monkeypatch.setattr(settings, "elevenlabs_v3_native_sample_rate", 8000)


@pytest.fixture
def logs():
    captured: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: captured.append((m.record["level"].name, m.record["message"])),
        level="DEBUG",
    )
    yield captured
    logger.remove(sink)


@pytest.fixture
def provider(monkeypatch):
    connect = DroppableConnect()

    def _fake_connect(uri, additional_headers=None, open_timeout=None):
        return connect(uri, additional_headers or {})

    monkeypatch.setattr(elevenlabs_pool, "connect", _fake_connect)
    p = ElevenLabsProvider(api_key="k", base_url=BASE)
    p.connect = connect  # type: ignore[attr-defined]
    return p


def _synth(provider: ElevenLabsProvider) -> asyncio.Task:
    return asyncio.create_task(
        provider.synth(
            text=TEXT, voice_id=VOICE, model=V3_MODEL, language="hi-IN", params={}
        )
    )


async def _flushed_ctx(socket: DroppableSocket) -> str:
    await _wait_for(lambda: any(m.get("flush") for m in socket.sent))
    return next(m["context_id"] for m in socket.sent if m.get("flush"))


def _audio(ctx: str, data: bytes) -> dict:
    return {"context_id": ctx, "audio": base64.b64encode(data).decode()}


async def test_dropped_socket_is_retried_on_another_socket(provider, logs):
    connect = provider.connect
    try:
        task = _synth(provider)
        await _wait_for(lambda: len(connect.sockets) == 1)
        first = connect.sockets[0]
        ctx = await _flushed_ctx(first)
        first.feed(_audio(ctx, b"PARTIAL"))  # some audio, then the network drops
        first.drop()

        await _wait_for(lambda: len(connect.sockets) == 2)
        second = connect.sockets[1]
        ctx2 = await _flushed_ctx(second)
        assert ctx2 != ctx, "the retry is a fresh context"
        second.feed(_audio(ctx2, b"FULL"))
        second.feed({"context_id": ctx2, "is_final_audio_for_turn": True})
        result = await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()

    # ONLY the retry's audio: the failed attempt's partial is discarded.
    assert result.audio == b"FULL"
    warned = [m for lvl, m in logs if lvl == "WARNING" and "retrying once" in m]
    assert len(warned) == 1 and "no close frame received or sent" in warned[0]
    assert any(lvl == "INFO" and "retry succeeded" in m for lvl, m in logs)
    assert not any(lvl == "ERROR" for lvl, _ in logs)


async def test_retry_failure_raises_and_logs_the_text(provider, logs):
    connect = provider.connect
    try:
        task = _synth(provider)
        await _wait_for(lambda: len(connect.sockets) == 1)
        await _flushed_ctx(connect.sockets[0])
        connect.sockets[0].drop()
        await _wait_for(lambda: len(connect.sockets) == 2)
        await _flushed_ctx(connect.sockets[1])
        connect.sockets[1].drop()

        with pytest.raises(ProviderError, match="no close frame received or sent"):
            await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()

    flushes = sum(1 for s in connect.sockets for m in s.sent if m.get("flush"))
    assert flushes == 2, "exactly one retry — no third attempt"
    errors = [m for lvl, m in logs if lvl == "ERROR" and "failed after retry" in m]
    assert len(errors) == 1
    assert repr(TEXT) in errors[0], "the lost sentence is logged in full"


async def test_cancellation_is_not_retried(provider, logs):
    connect = provider.connect
    try:
        task = _synth(provider)
        await _wait_for(lambda: len(connect.sockets) == 1)
        await _flushed_ctx(connect.sockets[0])
        task.cancel()  # caller hung up / barge-in
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.1)
    finally:
        await provider.aclose()

    flushes = sum(1 for s in connect.sockets for m in s.sent if m.get("flush"))
    assert flushes == 1
    assert not any("retrying once" in m for _, m in logs)


async def test_first_try_success_does_not_retry(provider, logs):
    connect = provider.connect
    try:
        task = _synth(provider)
        await _wait_for(lambda: len(connect.sockets) == 1)
        ctx = await _flushed_ctx(connect.sockets[0])
        connect.sockets[0].feed(_audio(ctx, b"OK"))
        connect.sockets[0].feed({"context_id": ctx, "is_final_audio_for_turn": True})
        result = await asyncio.wait_for(task, timeout=3.0)
    finally:
        await provider.aclose()

    assert result.audio == b"OK"
    assert len(connect.sockets) == 1
    assert not any("retry" in m for _, m in logs)
