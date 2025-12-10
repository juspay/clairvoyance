"""ElevenLabs TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

__all__ = ["ElevenLabsConfig", "build_elevenlabs_tts"]


@dataclass
class ElevenLabsConfig:
    """Configuration for ElevenLabs TTS."""

    api_key: str
    voice_id: str
    model_id: str
    speed: float = 1.0
    language: Language = Language.EN_IN
    text_filters: Optional[Sequence] = None


def build_elevenlabs_tts(config: ElevenLabsConfig):
    """Create an ElevenLabs TTS service."""

    text_filters = list(config.text_filters) if config.text_filters else None

    return ElevenLabsTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model_id=config.model_id,
        params=ElevenLabsTTSService.InputParams(
            speed=config.speed,
            language=config.language,
        ),
        text_filters=text_filters,
    )
