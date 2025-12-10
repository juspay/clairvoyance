"""Sarvam TTS helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = ["SarvamTTSConfig", "get_sarvam_language", "build_sarvam_tts"]


@dataclass
class SarvamTTSConfig:
    """Configuration for Sarvam TTS."""

    api_key: str
    voice_id: str
    model: str
    pitch: float
    pace: float
    language_code: Optional[str] = None


def get_sarvam_language(language_code: Optional[str]) -> Language:
    """Convert SARVAM language code to :class:`Language` for TTS.

    Falls back to ``Language.EN_IN`` if an invalid or missing code is provided.
    """

    if language_code:
        try:
            return Language(language_code)
        except ValueError:
            logger.warning(
                "Invalid TTS language code: %s, falling back to EN_IN", language_code
            )

    logger.warning("No SARVAM TTS language code provided, falling back to EN_IN")
    return Language.EN_IN


def build_sarvam_tts(config: SarvamTTSConfig):
    """Create a Sarvam TTS service."""

    language = get_sarvam_language(config.language_code)

    logger.info(
        f"Using Sarvam TTS service with model={config.model}, voice_id={config.voice_id}, language={language}, pitch={config.pitch}, pace={config.pace}"
    )

    return SarvamTTSService(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        params=SarvamTTSService.InputParams(
            language=language,
            pitch=config.pitch,
            pace=config.pace,
        ),
    )
