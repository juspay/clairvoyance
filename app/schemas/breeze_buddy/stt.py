"""Schemas for the standalone (template-independent) STT endpoints."""

import json
from typing import List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

from app.ai.voice.agents.breeze_buddy.template.types import (
    DeepgramSTTConfig,
    SarvamSTTConfig,
    SonioxSTTConfig,
    STTProvider,
)

# Cap on a single provider-config JSON form field. Real configs are well
# under 1 KiB; the cap bounds json.loads work on hostile multipart input.
_MAX_CONFIG_JSON_BYTES = 64 * 1024


class TranscriptionRequest(BaseModel):
    """Form fields of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    ``provider`` selects the STT provider; ``model`` optionally overrides that
    provider's default model, and ``language`` is a BCP-47 / ISO-639 hint.
    Values are trimmed; a blank ``model``/``language`` means "use the default".

    Provider-specific tuning goes in the matching nested config (``soniox`` /
    ``deepgram`` / ``sarvam``) — the same models templates use (e.g. Soniox
    ``context``, Deepgram ``smart_format``/``numerals``, Sarvam
    ``language_code``). In multipart form data these arrive as JSON strings.
    A model explicitly set in the selected provider's nested config wins over
    the flat ``model`` shortcut; otherwise the flat value fills in. Configs
    for other providers are ignored.
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
            if len(stripped) > _MAX_CONFIG_JSON_BYTES:
                raise ValueError(
                    f"provider config exceeds {_MAX_CONFIG_JSON_BYTES} bytes"
                )
            try:
                return json.loads(stripped)
            except RecursionError:
                # json.loads recurses per nesting level; pathological input
                # (~1000 deep) raises RecursionError, which Pydantic would
                # NOT convert to a validation error — it would surface as an
                # unhandled 500. Re-raise as ValueError so it becomes a 422
                # like any other bad JSON.
                raise ValueError("JSON too deeply nested") from None
        return value

    @model_validator(mode="after")
    def _refuse_a_sarvam_window(self) -> "TranscriptionRequest":
        """A one-shot clip has no streaming VAD: a Sarvam silence window
        would be accepted and silently do nothing."""
        if self.sarvam is not None and self.sarvam.negative_frames_count is not None:
            raise ValueError(
                "sarvam.negative_frames_count / negative_frames_window apply to "
                "streaming STT only, not /stt/transcribe"
            )
        return self


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
    ``smart_format``). A model explicitly set in the selected provider's
    nested config wins over the flat ``model`` shortcut; otherwise the flat
    value fills in. Configs for other providers are ignored.
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

    @model_validator(mode="before")
    @classmethod
    def _flat_model_into_deepgram(cls, data: object) -> object:
        """Put the flat ``model`` inside the deepgram block before it is
        validated: its Nova/Flux field check must judge the model actually
        used, or Flux settings are refused as Nova ones."""
        if not isinstance(data, dict):
            return data
        deepgram = data.get("deepgram")
        raw_model = data.get("model")
        # Trimmed the way the flat field is trimmed later (_blank_to_none): a
        # blank model must leave Deepgram's default in place, and a padded
        # " flux-general-multi " must still read as Flux.
        model = raw_model.strip() if isinstance(raw_model, str) else None
        provider = str(data.get("provider") or "").strip().lower()
        if (
            provider == "deepgram"
            and model
            and isinstance(deepgram, dict)
            and "model" not in deepgram
        ):
            return {**data, "deepgram": {**deepgram, "model": model}}
        return data

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
    Whisper (streaming-only provider, missing API key, or transient failure) —
    but only for provider-default requests: an explicit ``model`` or nested
    provider config pins the request, and provider failure then returns 502
    instead of degrading to Whisper.
    """

    text: str
    provider: str
    model: Optional[str] = None
