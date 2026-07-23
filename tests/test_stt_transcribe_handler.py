"""Tests for the standalone, template-independent STT handler."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile

from app.api.routers.breeze_buddy.stt import handlers
from app.schemas import UserInfo
from app.schemas.breeze_buddy.auth import UserRole
from app.schemas.breeze_buddy.stt import TranscriptionRequest

_USER = UserInfo(id="user-1", username="tester", role=UserRole.ADMIN)


def test_request_normalizes_provider_and_blank_fields() -> None:
    request = TranscriptionRequest(
        provider=" DEEPGRAM ", model=" nova-3 ", language="  "
    )
    assert request.provider.value == "deepgram"
    assert request.model == "nova-3"
    assert request.language is None


def test_request_parses_nested_config_from_json_string() -> None:
    request = TranscriptionRequest.model_validate(
        {"provider": "soniox", "soniox": '{"context": "brand names"}'}
    )
    assert request.soniox is not None
    assert request.soniox.context == "brand names"

    with pytest.raises(ValueError):
        TranscriptionRequest.model_validate(
            {"provider": "soniox", "soniox": "{not json"}
        )


def test_batch_model_and_options_resolution() -> None:
    request = TranscriptionRequest.model_validate(
        {"provider": "soniox", "model": "stt-async-v2", "soniox": {"context": "acme"}}
    )
    model, opts = handlers._batch_model_and_options(request)
    assert model == "stt-async-v2"
    assert opts == {"context": "acme"}

    request = TranscriptionRequest.model_validate(
        {
            "provider": "deepgram",
            "model": "ignored",
            "deepgram": {"model": "nova-3-medical", "numerals": False},
        }
    )
    model, opts = handlers._batch_model_and_options(request)
    assert model == "nova-3-medical"
    assert opts is not None
    assert opts["numerals"] is False
    assert "endpointing_ms" not in opts

    request = TranscriptionRequest.model_validate(
        {"provider": "sarvam", "sarvam": {"language_code": "hi-IN"}}
    )
    assert handlers._batch_model_and_options(request) == (
        None,
        {"language_code": "hi-IN"},
    )

    request = TranscriptionRequest.model_validate(
        {"provider": "openai", "model": "whisper-1"}
    )
    assert handlers._batch_model_and_options(request) == ("whisper-1", None)


@pytest.mark.parametrize(
    ("result_provider", "result_model"),
    [("deepgram", "nova-3"), ("openai", "whisper-1")],
)
async def test_response_reports_actual_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
    result_provider: str,
    result_model: str,
) -> None:
    monkeypatch.setattr(handlers, "STT_MAX_AUDIO_BYTES", AsyncMock(return_value=1024))
    monkeypatch.setattr(
        handlers,
        "transcribe_audio",
        AsyncMock(
            return_value=SimpleNamespace(
                text="hello", provider=result_provider, model=result_model
            )
        ),
    )

    response = await handlers.handle_transcription_request(
        UploadFile(BytesIO(b"audio"), filename="clip.webm"),
        TranscriptionRequest(provider="deepgram", model="nova-3"),
        _USER,
    )

    assert response.provider == result_provider
    assert response.model == result_model
