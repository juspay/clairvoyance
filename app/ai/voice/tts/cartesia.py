"""Cartesia TTS helpers and builder."""

from dataclasses import dataclass
from typing import Literal, Optional

import httpx
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.transcriptions.language import Language

from app.core.config.dynamic import (
    BB_CARTESIA_LANGUAGE,
    BB_CARTESIA_MODEL,
    BB_CARTESIA_VOICE_ID,
)
from app.core.config.static import CARTESIA_API_KEY
from app.core.logger import logger

__all__ = ["CartesiaConfig", "build_cartesia_tts", "_generate_cartesia_audio"]


@dataclass
class CartesiaConfig:
    """Configuration for Cartesia TTS.

    Cartesia TTS supports various parameters for voice customization:
    - api_key: Cartesia API key for authentication
    - voice_id: ID of the voice to use for synthesis
    - model: TTS model to use (default: "sonic-3")
    - language: Language to use for synthesis (default: Language.EN)
    - speed: Voice speed control for non-Sonic-3 models (literal values: "slow", "normal", "fast")
    - generation_config: Generation configuration for Sonic-3 models with:
        - volume: Volume multiplier [0.5, 2.0], default 1.0
        - speed: Speed multiplier [0.6, 1.5], default 1.0
        - emotion: Single emotion string (e.g., "neutral", "excited", "happy")
    - aggregate_sentences: Whether to aggregate sentences within the TTSService (default: True)
    """

    api_key: str
    voice_id: str
    model: str = "sonic-3"
    language: Language = Language.EN
    speed: Optional[Literal["slow", "normal", "fast"]] = None
    generation_config: Optional[GenerationConfig] = None
    aggregate_sentences: bool = True


def build_cartesia_tts(config: CartesiaConfig) -> CartesiaTTSService:
    """Create a Cartesia TTS service.

    Args:
        config: CartesiaConfig instance with TTS parameters

    Returns:
        Configured CartesiaTTSService instance
    """

    # Build input params
    params = CartesiaTTSService.InputParams(
        language=config.language,
        speed=config.speed,
        generation_config=config.generation_config,
    )

    return CartesiaTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        params=params,
        aggregate_sentences=config.aggregate_sentences,
    )


async def _generate_cartesia_audio(text: str) -> bytes:
    """Synthesize audio using Cartesia TTS API."""
    if not CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY is required for Mira voice")

    voice_id = await BB_CARTESIA_VOICE_ID()
    model = await BB_CARTESIA_MODEL()
    language = await BB_CARTESIA_LANGUAGE()

    url = "https://api.cartesia.ai/tts/bytes"
    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
        "Content-Type": "application/json",
    }

    payload = {
        "model_id": model,
        "transcript": text,
        "voice": {"mode": "id", "id": voice_id},
        "language": language or "en",
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
    }

    logger.info(f"Synthesizing greeting with Cartesia: {text[:50]}...")

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content
