"""ElevenLabs TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from pipecat.services.elevenlabs.tts import ElevenLabsTTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.text.base_text_aggregator import BaseTextAggregator

from app.ai.voice.agents.breeze_buddy.utils.hybrid_text_aggregator import (
    CharacterCountOnlyAggregator,
    HybridTextAggregator,
)
from app.core.logger import logger

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
    # NEW: Text aggregation configuration
    use_hybrid_aggregator: bool = True  # Enable 40-char + sentence buffering
    min_chars: int = 40  # Minimum characters before sending to TTS (subsequent chunks)
    max_chars: int = 200  # Maximum characters (safety net)
    enable_sentence_detection: bool = True  # Also split on sentence boundaries
    first_chunk_min_chars: int = 20  # Minimum characters for first chunk only (ultra-low latency)


def build_elevenlabs_tts(config: ElevenLabsConfig):
    """Create an ElevenLabs TTS service with custom text aggregation.

    By default, uses HybridTextAggregator which combines:
    - 40-character buffering (like Bolna) for low latency
    - Sentence-boundary detection (like default Pipecat) for natural audio

    This reduces latency from ~2.0-2.5s to ~1.0-1.5s while maintaining quality.
    """

    text_filters = list(config.text_filters) if config.text_filters else None

    # Create custom text aggregator
    text_aggregator: Optional[BaseTextAggregator] = None
    if config.use_hybrid_aggregator:
        if config.enable_sentence_detection:
            # Hybrid mode: fast first chunk (20 chars) + 40-char + sentence boundaries
            text_aggregator = HybridTextAggregator(
                first_chunk_min_chars=config.first_chunk_min_chars,
                min_chars=config.min_chars,
                max_chars=config.max_chars,
                enable_sentence_detection=True,
            )
            logger.info(
                f"Using HybridTextAggregator with first_chunk_min_chars={config.first_chunk_min_chars}, "
                f"min_chars={config.min_chars}, max_chars={config.max_chars}, sentence_detection=True"
            )
        else:
            # Character-count only mode (exactly like Bolna)
            text_aggregator = CharacterCountOnlyAggregator(
                buffer_size=config.min_chars
            )
            logger.info(
                f"Using CharacterCountOnlyAggregator with buffer_size={config.min_chars}"
            )
    else:
        # Use default SimpleTextAggregator (sentence-only, original behavior)
        logger.info("Using default SimpleTextAggregator (sentence-only)")

    return ElevenLabsTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model_id=config.model_id,
        params=ElevenLabsTTSService.InputParams(
            speed=config.speed,
            language=config.language,
        ),
        text_filters=text_filters,
        text_aggregator=text_aggregator
    )
