"""ElevenLabs TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

import httpx
from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language

from app.core.config.static import (
    ELEVENLABS_API_KEY,
    ELEVENLABS_BB_VOICE_ID,
    ELEVENLABS_INDIAN_RESIDENCY_API_KEY,
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
    model: str
    speed: float = 1.0
    language: Language = Language.EN_IN
    text_filters: Optional[Sequence] = None
    aggregate_sentences: bool = True
    enable_ssml_parsing: bool = False


def build_elevenlabs_tts(config: ElevenLabsConfig):
    """Create an ElevenLabs TTS service."""

    text_filters = list(config.text_filters) if config.text_filters else None

    return ElevenLabsTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        url=config.url,
        enable_ssml_parsing=config.enable_ssml_parsing,
        settings=ElevenLabsTTSService.Settings(
            speed=config.speed,
            language=config.language,
        ),
        text_filters=text_filters,
        text_aggregation_mode=(
            TextAggregationMode.SENTENCE
            if config.aggregate_sentences
            else TextAggregationMode.TOKEN
        ),
    )


async def _generate_elevenlabs_audio(
    text: str,
    voice_id: str | None = None,
    model_id: str | None = None,
    use_indian_residency: bool = True,
) -> bytes:
    """Synthesize audio using ElevenLabs TTS API.

    Args:
        text: The text to synthesize
        voice_id: Optional voice ID override. Falls back to ELEVENLABS_BB_VOICE_ID if None.
        model_id: Optional model ID override. Falls back to ELEVENLABS_MODEL_ID if None.
        use_indian_residency: If True, uses Indian residency API endpoint and key.
                             If False, uses default global API endpoint and key.
                             Defaults to True.

    Returns:
        Audio bytes in ulaw_8000 format
    """
    # Select API key and base URL based on residency preference
    if use_indian_residency:
        api_key = ELEVENLABS_INDIAN_RESIDENCY_API_KEY
        base_url = "https://api.in.residency.elevenlabs.io"
        if not api_key:
            raise ValueError(
                "ELEVENLABS_INDIAN_RESIDENCY_API_KEY is required when use_indian_residency is True"
            )
    else:
        api_key = ELEVENLABS_API_KEY
        base_url = "https://api.elevenlabs.io"
        if not api_key:
            raise ValueError("ELEVENLABS_API_KEY is required for Rhea voice")

    # Use provided values or fall back to defaults
    final_voice_id = voice_id if voice_id else ELEVENLABS_BB_VOICE_ID
    final_model_id = model_id if model_id else ELEVENLABS_MODEL_ID

    url = f"{base_url}/v1/text-to-speech/{final_voice_id}?output_format=ulaw_8000"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/basic",
    }

    payload = {
        "text": text,
        "model_id": final_model_id,
        "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
    }

    logger.info(
        f"Synthesizing greeting with ElevenLabs (ulaw_8000): {text[:50]}... "
        f"[voice_id={final_voice_id}, model_id={final_model_id}, indian_residency={use_indian_residency}]"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content
