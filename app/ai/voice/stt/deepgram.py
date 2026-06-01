"""Deepgram STT config and builder.

Uses pipecat's DeepgramSTTService with Nova-3 model.
VAD is handled by Silero in the pipeline — Deepgram's built-in vad_events
is deprecated (pipecat v0.0.99+) and not used here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions

from app.core.logger import logger

__all__ = ["DeepgramConfig", "build_deepgram_stt"]


@dataclass
class DeepgramConfig:
    """Configuration for Deepgram STT (Nova-3).

    Language configuration is determined automatically:
    - If auto_detect_language is True: uses "multi" for automatic detection
    - If auto_detect_language is False: uses the specified language code

    Latency-critical parameters:
    - endpointing: ms of silence before Deepgram finalises a transcript.
      Integer for exact ms (e.g. 25), True for Deepgram default (~480ms),
      False to disable.
    - utterance_end_ms: additional silence window (ms) after endpointing
      before UtteranceEnd event fires. None = disabled (no UtteranceEnd events).
    """

    api_key: str
    model: str = "nova-3-general"
    language: str = "en"
    auto_detect_language: bool = False
    smart_format: bool = False
    punctuate: bool = True
    endpointing: Union[int, bool] = True
    utterance_end_ms: int | None = None
    interim_results: bool = True
    profanity_filter: bool = False
    numerals: bool = True
    diarize: bool = False


def build_deepgram_stt(config: DeepgramConfig) -> DeepgramSTTService:
    """Create a Deepgram STT service.

    Automatically determines language configuration:
    - If auto_detect_language is True: uses "multi" for automatic detection
    - Otherwise: uses the configured language code

    Note: vad_events is intentionally omitted — deprecated in pipecat v0.0.99.
    Pipeline-level Silero VAD handles voice activity detection instead.
    """
    # Determine language configuration based on settings
    if config.auto_detect_language:
        language_config = "multi"  # Automatic detection
        logger.debug("Deepgram language auto-detection enabled (multi)")
    else:
        language_config = config.language  # Single language
        logger.debug(f"Deepgram using single language: {language_config}")

    live_opts: dict = dict(
        model=config.model,
        language=language_config,
        smart_format=config.smart_format,
        punctuate=config.punctuate,
        endpointing=config.endpointing,
        interim_results=config.interim_results,
        profanity_filter=config.profanity_filter,
        numerals=config.numerals,
        diarize=config.diarize,
    )
    if config.utterance_end_ms is not None:
        live_opts["utterance_end_ms"] = config.utterance_end_ms
    live_options = LiveOptions(**live_opts)

    logger.info(
        "Using Deepgram STT service with model: {}, language: {}, endpointing: {}, utterance_end_ms: {}",
        config.model,
        language_config,
        config.endpointing,
        config.utterance_end_ms,
    )
    return DeepgramSTTService(api_key=config.api_key, live_options=live_options)
