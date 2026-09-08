"""Sarvam STT helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = [
    "SarvamConfig",
    "get_sarvam_language",
    "build_sarvam_stt",
]


@dataclass
class SarvamConfig:
    """Configuration for Sarvam STT.

    pipecat 1.8 removed the `prompt` setting along with the sunset v2.5
    models; the remaining saaras models all accept a language.
    """

    api_key: str
    model: str
    sample_rate: int
    language_code: Optional[str] = None
    vad_signals: Optional[bool] = None
    high_vad_sensitivity: Optional[bool] = None


def get_sarvam_language(language_code: Optional[str]) -> Optional[Language]:
    """
    Convert SARVAM language code to :class:`Language` for STT.

    Args:
        language_code: Language code string (e.g., "en-IN", "hi-IN").

    Returns:
        Language enum value, or None if invalid/not provided.
    """
    if language_code:
        try:
            return Language(language_code)
        except ValueError:
            logger.warning(
                "Invalid STT language code: %s, returning None", language_code
            )
            return None

    logger.debug("No SARVAM STT language code provided, returning None")
    return None


def build_sarvam_stt(config: SarvamConfig):
    """Create a Sarvam STT service.

    Language is passed for every model; leave it unset to auto-detect.
    """
    language_param = get_sarvam_language(language_code=config.language_code)

    logger.info(
        f"Using Sarvam STT service with model={config.model}, language={'set' if language_param else 'none'}, vad_signals={config.vad_signals}, high_vad_sensitivity={config.high_vad_sensitivity}"
    )

    return SarvamSTTService(
        api_key=config.api_key,
        model=config.model,
        sample_rate=config.sample_rate,
        settings=SarvamSTTService.Settings(
            language=language_param,
            vad_signals=config.vad_signals if config.vad_signals is not None else True,
            high_vad_sensitivity=(
                config.high_vad_sensitivity
                if config.high_vad_sensitivity is not None
                else False
            ),
        ),
    )
