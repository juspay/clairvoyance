"""Request/response models — Cartesia /tts/bytes-shaped for drop-in compatibility.

``model_id`` carries routing as ``<provider>:<model>``. ``params`` and
``audio_base64`` are DragonTTS-only extensions (real Cartesia ignores unknown
fields): ``params`` is reserved for phase-2 tuning, ``audio_base64`` lets
``/tts/create`` store pre-supplied audio without calling a provider.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CartesiaVoice(BaseModel):
    mode: str = "id"
    id: str


class OutputFormat(BaseModel):
    container: str = "raw"
    encoding: str = "pcm_s16le"
    sample_rate: int = 16000


class TTSRequest(BaseModel):
    """Mirrors Cartesia's POST /tts/bytes body. Clients swap only the base URL."""

    model_id: str
    transcript: str
    voice: CartesiaVoice
    language: str = "en"
    output_format: OutputFormat = Field(default_factory=OutputFormat)
    params: dict = Field(default_factory=dict)
    audio_base64: str | None = None  # /tts/create extension: pre-supplied audio

    @field_validator("params")
    @classmethod
    def _validate_tempo(cls, v: dict) -> dict:
        # params.tempo is the ffmpeg atempo speaking-rate factor. It applies
        # ONLY to eleven_v3_conversational (other models get it stripped in
        # CacheService._resolve); the range is ffmpeg's atempo hard bounds.
        tempo = v.get("tempo")
        if tempo is not None:
            try:
                t = float(tempo)
            except (TypeError, ValueError):
                raise ValueError(
                    "params.tempo must be numeric (ffmpeg atempo factor, 0.5-2.0)"
                )
            if not 0.5 <= t <= 2.0:
                raise ValueError("params.tempo must be within 0.5-2.0 (atempo bounds)")
        return v
