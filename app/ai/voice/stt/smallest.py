"""Smallest.ai Pulse STT config and builder (pipecat's own SmallestSTTService).

Pulse endpoints server-side, so it produces finals under stt_native without a
local VAD; under smart_turn pipecat also sends ``finalize`` on
VADUserStoppedSpeakingFrame. Pulse documents no end-of-speech / silence
setting, so ``end_of_speech_ms`` is refused for this provider at template
parse.
"""

from __future__ import annotations

from dataclasses import dataclass

from pipecat.services.smallest.stt import SmallestSTTService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = ["SMALLEST_LANGUAGES", "SmallestConfig", "build_smallest_stt"]

# The base codes Pulse is verified for: the LANGUAGE_MAP inside pipecat
# 1.1.0's language_to_smallest_stt_language (a function-local dict, so it
# cannot be imported). Anything else pipecat forwards as a bare code with a
# warning, and a code Pulse rejects leaves the call with no transcripts.
SMALLEST_LANGUAGES = frozenset(
    "bg bn cs da de en es et fi fr gu hi hu it kn lt lv ml mr mt nl or pa pl "
    "pt ro ru sk sv ta te uk".split()
)


def smallest_supports(code: str) -> bool:
    """Whether Pulse is verified for this code (``hi`` or ``hi-IN``)."""
    return code.split("-")[0].lower() in SMALLEST_LANGUAGES


@dataclass
class SmallestConfig:
    """Configuration for Smallest Pulse STT."""

    api_key: str
    language: str = "hi"
    numerals: bool = True


def build_smallest_stt(config: SmallestConfig) -> SmallestSTTService:
    """Create a Smallest Pulse STT service.

    ``numerals`` writes spoken numbers as digits; pipecat's default ("auto")
    spelled a phone number out in English words on Hindi audio.
    """
    logger.info(
        "Using Smallest Pulse STT (language={}, numerals={})",
        config.language,
        config.numerals,
    )
    try:
        requested: Language | None = Language(config.language)
    except ValueError:
        requested = None
    if requested is not None and smallest_supports(config.language):
        language = requested
    else:
        # Reached only via the template's top-level language (the smallest
        # block's own field is validated at parse): degrade, don't drop the call.
        logger.warning(
            "Smallest: language '{}' not supported by Pulse, using 'hi'",
            config.language,
        )
        language = Language.HI
    return SmallestSTTService(
        api_key=config.api_key,
        settings=SmallestSTTService.Settings(
            language=language,
            numerals="true" if config.numerals else "false",
        ),
    )
