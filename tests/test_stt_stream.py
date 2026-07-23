"""Tests for the realtime STT stream: schema, WS auth shim, protocol edges."""

import json
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, WebSocket

from app.api.routers.breeze_buddy.stt import handlers
from app.api.security.breeze_buddy import rbac_token
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.stt import TranscriptionStreamRequest

_USER = UserInfo(id="user-1", username="tester", role=UserRole.ADMIN)


class FakeWebSocket:
    """Minimal stand-in implementing the surface the stream handler uses.

    ``messages`` scripts what ``receive()`` yields after the config message;
    once exhausted, a disconnect message is returned. ``fail_sends_after``
    makes ``send_text`` raise once that many messages have been sent.
    """

    def __init__(
        self,
        config_text: str,
        headers: dict | None = None,
        query: dict | None = None,
        messages: list[dict] | None = None,
        fail_sends_after: int | None = None,
    ):
        self._config_text = config_text
        self._messages = list(messages or [])
        self._fail_sends_after = fail_sends_after
        self.headers = headers or {}
        self.query_params = query or {}
        self.sent: list[dict] = []
        self.closed: tuple[int, str] | None = None
        self.accepted = False
        self.client_state = SimpleNamespace(name="CONNECTED")

    async def accept(self) -> None:
        self.accepted = True

    async def receive_text(self) -> str:
        return self._config_text

    async def receive(self) -> dict:
        if self._messages:
            return self._messages.pop(0)
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        if (
            self._fail_sends_after is not None
            and len(self.sent) >= self._fail_sends_after
        ):
            raise RuntimeError("send failed")
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self.client_state = SimpleNamespace(name="DISCONNECTED")


def as_ws(fake: FakeWebSocket) -> WebSocket:
    return cast(WebSocket, fake)


class StubTranscriber:
    """Replaces StreamingTranscriber so handler tests skip Pipecat entirely."""

    def __init__(self, stt_service, *, sample_rate, on_transcript):
        self.on_transcript = on_transcript
        self.fed: list[bytes] = []
        self.stopped = False
        self.failed = False
        self.start_error: str | None = None
        self.emit_on_feed = False

    async def start(self) -> None:
        if self.start_error:
            raise RuntimeError(self.start_error)

    async def feed(self, audio: bytes) -> None:
        self.fed.append(audio)
        if self.emit_on_feed:
            from app.ai.voice.stt.streaming import TranscriptEvent

            await self.on_transcript(TranscriptEvent(text="hi", is_final=True))

    async def stop(self) -> None:
        self.stopped = True


@pytest.fixture
def stream_env(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Patch auth, provider factory, limits, and the transcriber."""
    created: dict = {}

    def _make(stt_service, *, sample_rate, on_transcript):
        stub = StubTranscriber(
            stt_service, sample_rate=sample_rate, on_transcript=on_transcript
        )
        created["transcriber"] = stub
        for attr, value in created.get("presets", {}).items():
            setattr(stub, attr, value)
        return stub

    monkeypatch.setattr(handlers, "get_user_from_websocket", lambda ws: _USER)
    monkeypatch.setattr(
        handlers, "create_stt_from_config", AsyncMock(return_value=object())
    )
    monkeypatch.setattr(handlers, "STT_STREAM_MAX_SECONDS", AsyncMock(return_value=300))
    monkeypatch.setattr(
        handlers, "STT_STREAM_IDLE_TIMEOUT_SECONDS", AsyncMock(return_value=60)
    )
    monkeypatch.setattr(handlers, "StreamingTranscriber", _make)
    return created


def test_stream_request_normalizes_and_bounds() -> None:
    request = TranscriptionStreamRequest.model_validate(
        {"provider": " SONIOX ", "model": "  ", "language": ["en", " ", "hi "]}
    )
    assert request.provider.value == "soniox"
    assert request.model is None
    assert request.language == ["en", "hi"]
    assert request.sample_rate == 16000

    with pytest.raises(ValueError):
        TranscriptionStreamRequest.model_validate(
            {"provider": "soniox", "sample_rate": 4000}
        )


def test_websocket_auth_reads_header_then_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(
        rbac_token.rbac_token_manager,
        "verify_rbac_token",
        lambda token: seen.append(token) or _USER,
    )

    ws = FakeWebSocket("", headers={"authorization": "Bearer header-token"})
    assert rbac_token.get_user_from_websocket(as_ws(ws)) is _USER

    ws = FakeWebSocket("", query={"token": "query-token"})
    assert rbac_token.get_user_from_websocket(as_ws(ws)) is _USER
    assert seen == ["header-token", "query-token"]

    with pytest.raises(HTTPException):
        rbac_token.get_user_from_websocket(as_ws(FakeWebSocket("")))


async def test_stream_rejects_unauthenticated() -> None:
    ws = FakeWebSocket("{}")
    await handlers.handle_transcription_stream(as_ws(ws))

    assert ws.accepted
    assert ws.sent[-1]["type"] == "error"
    assert ws.closed is not None and ws.closed[0] == 4401


async def test_stream_rejects_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "get_user_from_websocket", lambda ws: _USER)

    ws = FakeWebSocket(json.dumps({"provider": "not-a-provider"}))
    await handlers.handle_transcription_stream(as_ws(ws))

    assert ws.sent[-1]["type"] == "error"
    assert ws.closed is not None and ws.closed[0] == 4400


async def test_stream_rejects_openai_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "get_user_from_websocket", lambda ws: _USER)

    ws = FakeWebSocket(json.dumps({"provider": "openai"}))
    await handlers.handle_transcription_stream(as_ws(ws))

    assert ws.sent[-1]["type"] == "error"
    assert "realtime" in ws.sent[-1]["message"]
    assert ws.closed is not None and ws.closed[0] == 4400


async def test_stream_rejects_sarvam_sample_rate_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(handlers, "get_user_from_websocket", lambda ws: _USER)

    ws = FakeWebSocket(json.dumps({"provider": "sarvam", "sample_rate": 8000}))
    await handlers.handle_transcription_stream(as_ws(ws))

    assert ws.sent[-1]["type"] == "error"
    assert "sample_rate" in ws.sent[-1]["message"]
    assert ws.closed is not None and ws.closed[0] == 4400


async def test_stream_rejects_provider_startup_failure(stream_env: dict) -> None:
    stream_env["presets"] = {"start_error": "connect refused"}

    ws = FakeWebSocket(json.dumps({"provider": "soniox"}))
    await handlers.handle_transcription_stream(as_ws(ws))

    assert all(event["type"] != "ready" for event in ws.sent)
    assert ws.sent[-1]["type"] == "error"
    assert "provider connection failed" in ws.sent[-1]["message"]
    assert ws.closed is not None and ws.closed[0] == 4400


async def test_stream_rejects_oversized_chunk(stream_env: dict) -> None:
    big = b"x" * (handlers._MAX_CHUNK_BYTES + 1)
    ws = FakeWebSocket(
        json.dumps({"provider": "soniox"}),
        messages=[{"type": "websocket.receive", "bytes": big}],
    )
    await handlers.handle_transcription_stream(as_ws(ws))

    assert stream_env["transcriber"].fed == []
    assert stream_env["transcriber"].stopped
    assert ws.sent[-1]["type"] == "error"
    assert "chunk exceeds" in ws.sent[-1]["message"]
    assert ws.closed is not None and ws.closed[0] == 4400


async def test_stream_feeds_audio_and_forwards_transcripts(stream_env: dict) -> None:
    stream_env["presets"] = {"emit_on_feed": True}
    chunk = b"\x00\x01" * 100
    ws = FakeWebSocket(
        json.dumps({"provider": "soniox"}),
        messages=[
            {"type": "websocket.receive", "bytes": chunk},
            {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
        ],
    )
    await handlers.handle_transcription_stream(as_ws(ws))

    assert stream_env["transcriber"].fed == [chunk]
    assert stream_env["transcriber"].stopped
    types = [event["type"] for event in ws.sent]
    assert types[0] == "ready"
    assert "final" in types
    assert ws.closed is not None and ws.closed[0] == 1000


async def test_stream_ends_when_client_send_fails(stream_env: dict) -> None:
    stream_env["presets"] = {"emit_on_feed": True}
    chunk = b"\x00\x01" * 100
    # Allow only the "ready" send; the transcript forward then fails.
    ws = FakeWebSocket(
        json.dumps({"provider": "soniox"}),
        messages=[
            {"type": "websocket.receive", "bytes": chunk},
            {"type": "websocket.receive", "bytes": chunk},
        ],
        fail_sends_after=1,
    )
    await handlers.handle_transcription_stream(as_ws(ws))

    assert stream_env["transcriber"].stopped
    assert ws.closed is not None and ws.closed[0] == 1011
    assert ws.closed[1] == "client send failed"
