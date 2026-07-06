"""Schemas for the global (template-independent) STT endpoints."""

from typing import Optional

from pydantic import BaseModel


class GlobalTranscribeResponse(BaseModel):
    """Body of ``POST /agent/voice/breeze-buddy/stt/transcribe``.

    One-shot transcription decoupled from any widget session or template: the
    caller supplies the audio clip plus the ``provider``/``model`` to use.
    ``provider`` is the STT provider that actually produced the text (may differ
    from the requested one when a streaming-only provider falls back to Whisper).
    ``model`` echoes back the requested override only when that requested provider
    produced the transcript (``None`` when the provider default was used, or when
    the request fell back to a different provider).
    """

    text: str
    provider: str
    model: Optional[str] = None
