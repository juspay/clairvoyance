"""Google TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from pipecat.services.google.tts import GoogleTTSService
from pipecat.transcriptions.language import Language

__all__ = ["GoogleConfig", "build_google_tts"]


@dataclass
class GoogleConfig:
    """Configuration for Google TTS."""

    voice_id: str
    language: Language = Language.EN_IN
    credentials: Optional[Any] = None
    text_filters: Optional[Sequence] = None


def build_google_tts(config: GoogleConfig):
    """Create a Google TTS service."""

    text_filters = list(config.text_filters) if config.text_filters else None

    return GoogleTTSService(
        voice_id=config.voice_id,
        params=GoogleTTSService.InputParams(language=config.language),
        credentials=config.credentials,
        text_filters=text_filters,
    )
