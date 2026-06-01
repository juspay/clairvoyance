"""Cartesia TTS helpers and builder."""

from dataclasses import dataclass
from typing import Optional

import httpx
from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.services.tts_service import TextAggregationMode
from pipecat.transcriptions.language import Language

from app.core.config.dynamic import BB_VOICE_PROVIDER_DEFAULTS
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
    - generation_config: Generation configuration for Sonic-3 models with:
        - volume: Volume multiplier [0.5, 2.0], default 1.0
        - speed: Speed multiplier [0.6, 1.5], default 1.0
        - emotion: Single emotion string (e.g., "neutral", "excited", "happy")
    - aggregate_sentences: Whether to aggregate sentences within the TTSService (default: True)
    """

    api_key: str
    voice_id: str
    model: str = "sonic-3.5"
    language: Language = Language.EN
    generation_config: Optional[GenerationConfig] = None
    aggregate_sentences: bool = True


def build_cartesia_tts(config: CartesiaConfig) -> CartesiaTTSService:
    """Create a Cartesia TTS service.

    Args:
        config: CartesiaConfig instance with TTS parameters

    Returns:
        Configured CartesiaTTSService instance
    """

    return CartesiaTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        settings=CartesiaTTSService.Settings(
            language=config.language,
            generation_config=config.generation_config,
        ),
        text_aggregation_mode=(
            TextAggregationMode.SENTENCE
            if config.aggregate_sentences
            else TextAggregationMode.TOKEN
        ),
    )


async def _generate_cartesia_audio(
    text: str,
    voice_id: str | None = None,
    model: str | None = None,
) -> bytes:
    """Synthesize audio using Cartesia TTS API.

    Args:
        text: The text to synthesize
        voice_id: Optional voice ID override. Falls back to Redis defaults if None.
        model: Optional model override. Falls back to Redis defaults if None.

    Returns:
        Audio bytes in raw PCM format (16-bit, 16kHz)
    """
    if not CARTESIA_API_KEY:
        raise ValueError("CARTESIA_API_KEY is required for Cartesia TTS")

    # Use provided values or fall back to Redis/hardcoded defaults
    defaults = await BB_VOICE_PROVIDER_DEFAULTS("cartesia")
    final_voice_id = voice_id or defaults.get("voice_id", "")
    final_model = model or defaults.get("model", "sonic-3.5")
    language = defaults.get("language", "en")

    url = "https://api.cartesia.ai/tts/bytes"
    headers = {
        "X-API-Key": CARTESIA_API_KEY,
        "Cartesia-Version": "2024-06-10",
        "Content-Type": "application/json",
    }

    payload = {
        "model_id": final_model,
        "transcript": text,
        "voice": {"mode": "id", "id": final_voice_id},
        "language": language,
        "output_format": {
            "container": "raw",
            "encoding": "pcm_s16le",
            "sample_rate": 16000,
        },
    }

    logger.info(
        f"Synthesizing audio with Cartesia: {text[:50]}... "
        f"[voice_id={final_voice_id}, model={final_model}]"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, headers=headers, timeout=30.0)
        response.raise_for_status()
        return response.content
