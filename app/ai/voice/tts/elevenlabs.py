"""ElevenLabs TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import httpx
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language

from app.core.config.static import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_BB_VOICE_ID,
    ELEVENLABS_MODEL_ID,
)
from app.core.logger import logger

__all__ = ["ElevenLabsConfig", "build_elevenlabs_tts", "_generate_elevenlabs_audio"]


@dataclass
class ElevenLabsConfig:
    """Configuration for ElevenLabs TTS."""

    api_key: str
    url: str
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
        url=config.url,
        params=ElevenLabsTTSService.InputParams(
            speed=config.speed,
            language=config.language,
        ),
        text_filters=text_filters,
    )


async def _generate_elevenlabs_audio(text: str) -> bytes:
    """Synthesize audio using ElevenLabs TTS API."""
    if not ELEVENLABS_API_KEY:
        raise ValueError("ELEVENLABS_API_KEY is required for Rhea voice")

    voice_id = ELEVENLABS_BB_VOICE_ID
    model_id = ELEVENLABS_MODEL_ID

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=ulaw_8000"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/basic",
    }

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    logger.info(f"Synthesizing greeting with ElevenLabs (ulaw_8000): {text[:50]}...")

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content
