"""Tests for the standalone, template-independent STT handler."""

from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

from app.ai.voice.stt import transcribe
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
    assert opts == {"numerals": False}

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

    # Deepgram nested config WITHOUT an explicit model: the flat model must
    # win over the schema default ("nova-3-general"), same as other providers.
    request = TranscriptionRequest.model_validate(
        {
            "provider": "deepgram",
            "model": "nova-3-medical",
            "deepgram": {"numerals": False},
        }
    )
    model, opts = handlers._batch_model_and_options(request)
    assert model == "nova-3-medical"
    assert opts is not None and opts["numerals"] is False


def test_hostile_json_config_is_422_not_500() -> None:
    deep = "[" * 1500 + "]" * 1500
    with pytest.raises(ValidationError):
        TranscriptionRequest.model_validate({"provider": "soniox", "soniox": deep})

    huge = '{"context": "' + "x" * (70 * 1024) + '"}'
    with pytest.raises(ValidationError):
        TranscriptionRequest.model_validate({"provider": "soniox", "soniox": huge})


async def test_strict_mode_does_not_fall_back_to_whisper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcribe, "SONIOX_API_KEY", "key")
    monkeypatch.setattr(transcribe, "OPENAI_STT_API_KEY", "key")
    monkeypatch.setattr(
        transcribe, "_soniox", AsyncMock(side_effect=RuntimeError("boom"))
    )
    whisper = AsyncMock(
        return_value=transcribe.Transcription(
            text="hi", provider="openai", model="whisper-1"
        )
    )
    monkeypatch.setattr(transcribe, "_openai", whisper)

    # Explicit model pin: provider failure must raise, never degrade.
    with pytest.raises(transcribe.TranscriptionError):
        await transcribe.transcribe_audio(
            b"x", None, provider="soniox", model="stt-rt-v4"
        )
    whisper.assert_not_awaited()

    # Provider options pin behaves the same.
    with pytest.raises(transcribe.TranscriptionError):
        await transcribe.transcribe_audio(
            b"x", None, provider="soniox", options={"context": "acme"}
        )
    whisper.assert_not_awaited()

    # Provider-default requests keep the fail-open Whisper behavior.
    result = await transcribe.transcribe_audio(b"x", None, provider="soniox")
    assert result.provider == "openai"
    whisper.assert_awaited()


async def test_strict_mode_errors_when_provider_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcribe, "SONIOX_API_KEY", "")
    monkeypatch.setattr(transcribe, "OPENAI_STT_API_KEY", "key")
    whisper = AsyncMock(
        return_value=transcribe.Transcription(
            text="hi", provider="openai", model="whisper-1"
        )
    )
    monkeypatch.setattr(transcribe, "_openai", whisper)

    with pytest.raises(transcribe.TranscriptionError):
        await transcribe.transcribe_audio(
            b"x", None, provider="soniox", model="stt-rt-v4"
        )
    whisper.assert_not_awaited()


async def test_google_model_override_rejected_with_400() -> None:
    with pytest.raises(HTTPException) as exc:
        await handlers.handle_transcription_request(
            UploadFile(BytesIO(b"audio"), filename="clip.webm"),
            TranscriptionRequest(provider="google", model="latest_long"),
            _USER,
        )
    assert exc.value.status_code == 400


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
