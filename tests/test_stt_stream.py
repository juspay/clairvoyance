"""Tests for the realtime STT stream: schema, WS auth shim, protocol edges."""

import json
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException, WebSocket

from app.api.routers.breeze_buddy.stt import handlers
from app.api.security.breeze_buddy import rbac_token
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.stt import TranscriptionStreamRequest

_USER = UserInfo(id="user-1", username="tester", role=UserRole.ADMIN)


class FakeWebSocket:
    """Minimal stand-in implementing the surface the stream handler uses."""

    def __init__(
        self,
        config_text: str,
        headers: dict | None = None,
        query: dict | None = None,
    ):
        self._config_text = config_text
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

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self.client_state = SimpleNamespace(name="DISCONNECTED")


def as_ws(fake: FakeWebSocket) -> WebSocket:
    return cast(WebSocket, fake)


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


async def test_stream_rejects_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
