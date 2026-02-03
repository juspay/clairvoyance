"""Deepgram STT config and builder."""

from __future__ import annotations

from dataclasses import dataclass

from deepgram import LiveOptions
from pipecat.services.deepgram.stt import DeepgramSTTService

from app.core.logger import logger

__all__ = ["DeepgramConfig", "build_deepgram_stt"]


@dataclass
class DeepgramConfig:
    """Configuration for Deepgram STT.

    Language configuration is determined automatically:
    - If auto_detect_language is True: uses "multi" for automatic detection
    - If auto_detect_language is False: uses the specified language code
    """

    api_key: str
    model: str
    language: str
    auto_detect_language: bool
    smart_format: bool
    punctuate: bool
    endpointing: int
    vad_events: bool
    utterance_end_ms: int
    no_delay: bool
    interim_results: bool
    profanity_filter: bool
    numerals: bool
    diarize: bool


def build_deepgram_stt(config: DeepgramConfig):
    """Create a Deepgram STT service.

    Automatically determines language configuration:
    - If auto_detect_language is True: uses "multi" for automatic detection
    - Otherwise: uses the configured language code
    """
    # Determine language configuration based on settings
    if config.auto_detect_language:
        language_config = "multi"  # Automatic detection
        logger.debug("Deepgram language auto-detection enabled (multi)")
    else:
        language_config = config.language  # Single language
        logger.debug(f"Deepgram using single language: {language_config}")

    live_options = LiveOptions(
        model=config.model,
        language=language_config,
        smart_format=config.smart_format,
        punctuate=config.punctuate,
        endpointing=config.endpointing,
        vad_events=config.vad_events,
        utterance_end_ms=str(config.utterance_end_ms),
        no_delay=config.no_delay,
        interim_results=config.interim_results,
        profanity_filter=config.profanity_filter,
        numerals=config.numerals,
        diarize=config.diarize,
    )

    logger.info(
        "Using Deepgram STT service with model: %s, language: %s (VAD: %s, Endpointing: %s)",
        config.model,
        language_config,
        config.vad_events,
        config.endpointing,
    )
    return DeepgramSTTService(api_key=config.api_key, live_options=live_options)
