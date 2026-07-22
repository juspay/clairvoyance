"""Schemas for the standalone (template-independent) STT endpoints."""

from typing import Optional

from pydantic import BaseModel, field_validator

from app.ai.voice.agents.breeze_buddy.template.types import STTProvider


class TranscriptionRequest(BaseModel):
    """Form fields of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    ``provider`` selects the STT provider; ``model`` optionally overrides that
    provider's default model, and ``language`` is a BCP-47 / ISO-639 hint.
    Values are trimmed; a blank ``model``/``language`` means "use the default".
    """

    provider: STTProvider
    model: Optional[str] = None
    language: Optional[str] = None

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


class TranscriptionResponse(BaseModel):
    """Body of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    ``provider`` and ``model`` report what actually produced the transcript.
    They may differ from the request when the shared core falls back to OpenAI
    Whisper (streaming-only provider, missing API key, or transient failure).
    """

    text: str
    provider: str
    model: Optional[str] = None
