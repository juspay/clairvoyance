"""Tests for the global, template-independent STT handler."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile

from app.api.routers.breeze_buddy.stt import handlers


@pytest.mark.parametrize(
    ("result_provider", "expected_model"),
    [("deepgram", "nova-3"), ("openai", None)],
)
async def test_model_override_only_matches_requested_provider(
    monkeypatch: pytest.MonkeyPatch,
    result_provider: str,
    expected_model: str | None,
) -> None:
    monkeypatch.setattr(
        handlers, "WIDGET_STT_MAX_AUDIO_BYTES", AsyncMock(return_value=1024)
    )
    monkeypatch.setattr(
        handlers,
        "transcribe_audio",
        AsyncMock(return_value=SimpleNamespace(text="hello", provider=result_provider)),
    )

    response = await handlers.transcribe_audio_handler(
        UploadFile(BytesIO(b"audio"), filename="clip.webm"),
        provider=" DEEPGRAM ",
        model=" nova-3 ",
        language=None,
    )

    assert response.provider == result_provider
    assert response.model == expected_model
