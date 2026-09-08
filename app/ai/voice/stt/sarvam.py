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

    The model type determines which parameters are used:
    - 'saaras' models (STT-Translate): auto-detect language
    - 'saarika' models (pure STT): use language parameter

    ``prompt`` is retained for template compatibility but is no longer sent:
    the installed pipecat's batch ``SarvamSTTService`` no longer accepts it —
    the parameter now lives only on the separate ``SarvamRealtimeSTTService``.
    ``build_sarvam_stt`` warns when one is set.
    """

    api_key: str
    model: str
    sample_rate: int
    language_code: Optional[str] = None
    prompt: Optional[str] = None
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

    Automatically determines which parameters to use based on the model type:
    - 'saaras' models (STT-Translate): accepts prompt, auto-detects language
    - 'saarika' models (pure STT): accepts language, ignores prompt
    """
    # Initialize parameters based on model type
    prompt_param = None
    language_param = None

    if "saaras" in config.model.lower():
        # STT-Translate model: no language (auto-detects). The prompt is
        # recorded but no longer deliverable — see the warning below.
        prompt_param = config.prompt if config.prompt else None
        logger.debug(
            f"Saaras model detected: using prompt={'set' if prompt_param else 'none'}, language auto-detection enabled"
        )
    else:
        # saarika (pure STT) model: accepts language, no prompt
        language_param = get_sarvam_language(language_code=config.language_code)
        logger.debug(
            f"Saarika model detected: using language={'set' if language_param else 'none'}, prompt disabled"
        )

    if prompt_param:
        # The installed pipecat's batch SarvamSTTService no longer accepts
        # `prompt` (nor exposes `set_prompt()`); the parameter now lives only
        # on the separate SarvamRealtimeSTTService. Templates configuring a
        # Saaras prompt are therefore no longer honoured. Warn rather than
        # drop it silently — restoring it means moving this builder to the
        # realtime service.
        logger.warning(
            "Sarvam STT prompt is configured but the installed pipecat no "
            "longer accepts it on SarvamSTTService; transcription will run "
            "without it (model={})",
            config.model,
        )

    logger.info(
        f"Using Sarvam STT service with model={config.model}, language={'set' if language_param else 'none'}, prompt={'set (IGNORED)' if prompt_param else 'none'}, vad_signals={config.vad_signals}, high_vad_sensitivity={config.high_vad_sensitivity}"
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
