"""Schemas for the standalone (template-independent) STT endpoints."""

import json
from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator

from app.ai.voice.agents.breeze_buddy.template.types import (
    DeepgramSTTConfig,
    SarvamSTTConfig,
    SonioxSTTConfig,
    STTProvider,
)


class TranscriptionRequest(BaseModel):
    """Form fields of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    ``provider`` selects the STT provider; ``model`` optionally overrides that
    provider's default model, and ``language`` is a BCP-47 / ISO-639 hint.
    Values are trimmed; a blank ``model``/``language`` means "use the default".

    Provider-specific tuning goes in the matching nested config (``soniox`` /
    ``deepgram`` / ``sarvam``) — the same models templates use (e.g. Soniox
    ``context``, Deepgram ``smart_format``/``numerals``, Sarvam
    ``language_code``). In multipart form data these arrive as JSON strings.
    When the selected provider's nested config is present it takes precedence
    over the flat ``model`` shortcut; configs for other providers are ignored.
    """

    provider: STTProvider
    model: Optional[str] = None
    language: Optional[str] = None
    soniox: Optional[SonioxSTTConfig] = None
    deepgram: Optional[DeepgramSTTConfig] = None
    sarvam: Optional[SarvamSTTConfig] = None

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("model", "language", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("soniox", "deepgram", "sarvam", mode="before")
    @classmethod
    def _parse_json_form_field(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return json.loads(stripped)
        return value


class TranscriptionStreamRequest(BaseModel):
    """First (JSON text) message on ``WS /agent/voice/breeze-buddy/stt/stream``.

    After this message the client sends binary frames of raw PCM16 mono audio
    at ``sample_rate``. ``language`` accepts a single code or a list (list is
    provider-dependent, e.g. Soniox hints). OpenAI has no realtime streaming
    path and is rejected; Sarvam streams are pinned to the platform rate.

    Provider-specific tuning goes in the matching nested config (``soniox`` /
    ``deepgram`` / ``sarvam``) — the same models templates use, passed through
    to the streaming service builders verbatim (e.g. Soniox ``context`` and
    ``enable_language_identification``, Deepgram ``endpointing_ms`` and
    ``smart_format``). When the selected provider's nested config is present
    it takes precedence over the flat ``model`` shortcut; configs for other
    providers are ignored.
    """

    provider: STTProvider
    model: Optional[str] = None
    language: Optional[Union[str, List[str]]] = None
    sample_rate: int = Field(
        16000,
        ge=8000,
        le=48000,
        description="Sample rate (Hz) of the PCM16 mono audio frames.",
    )
    soniox: Optional[SonioxSTTConfig] = None
    deepgram: Optional[DeepgramSTTConfig] = None
    sarvam: Optional[SarvamSTTConfig] = None

    @field_validator("provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("model", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("language", mode="before")
    @classmethod
    def _blank_language_to_none(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, list):
            cleaned = [v.strip() for v in value if isinstance(v, str) and v.strip()]
            return cleaned or None
        return value


class TranscriptionResponse(BaseModel):
    """Body of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    ``provider`` and ``model`` report what actually produced the transcript.
    They may differ from the request when the shared core falls back to OpenAI
    Whisper (streaming-only provider, missing API key, or transient failure).
    """

    text: str
    provider: str
    model: Optional[str] = None
