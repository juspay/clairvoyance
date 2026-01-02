"""Cartesia TTS helpers and builder."""

from dataclasses import dataclass
from typing import Literal, Optional

from pipecat.services.cartesia.tts import CartesiaTTSService, GenerationConfig
from pipecat.transcriptions.language import Language

__all__ = ["CartesiaConfig", "build_cartesia_tts"]


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
