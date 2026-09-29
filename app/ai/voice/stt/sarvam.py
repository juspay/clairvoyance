"""Sarvam STT helpers and builder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pipecat.services.sarvam.stt import MODEL_CONFIGS, SarvamSTTService
from pipecat.transcriptions.language import Language

from app.core.logger import logger

__all__ = [
    "SarvamConfig",
    "SarvamSTTServiceWithInterruptionPolicy",
    "get_sarvam_language",
    "build_sarvam_stt",
]


@dataclass
class SarvamConfig:
    """Configuration for Sarvam STT.

    Which of ``language`` / ``prompt`` reaches Sarvam is decided by what the
    model accepts (pipecat's ``MODEL_CONFIGS``), not by its family name:
    saaras:v3 takes a language and rejects a prompt, saaras:v2.5 the reverse,
    saarika takes a language. An unset language means Sarvam auto-detects.
    """

    api_key: str
    model: str
    sample_rate: int
    language_code: Optional[str] = None
    prompt: Optional[str] = None
    vad_signals: Optional[bool] = None
    high_vad_sensitivity: Optional[bool] = None
    self_interrupt: bool = True


class SarvamSTTServiceWithInterruptionPolicy(SarvamSTTService):
    """pipecat's SarvamSTTService, with its own barge-in made optional.

    Sarvam streams no partial words: only a START_SPEECH signal and the
    finished text of each segment. On START_SPEECH pipecat interrupts the bot
    itself, on the first sound, so a template's ``interruption.min_words``
    ("haan" must not stop the bot) never gets a say. With
    ``self_interrupt=False`` the signal is still broadcast, but the
    interruption is left to the pipeline's rule, which then judges the
    finished segment when the caller pauses.
    """

    def __init__(self, *, self_interrupt: bool = True, **kwargs):
        super().__init__(**kwargs)
        self._self_interrupt = self_interrupt

    def set_self_interrupt(self, enabled: bool) -> None:
        """Follow a node-level interruption override mid-call."""
        self._self_interrupt = enabled

    async def broadcast_interruption(self):
        # pipecat 1.1.0's Sarvam service calls this only on START_SPEECH.
        if self._self_interrupt:
            await super().broadcast_interruption()


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

    Sends ``language`` and ``prompt`` only to a model that accepts them.
    Previously every ``saaras`` model dropped the language, so saaras:v3
    always auto-detected: on noisy 8 kHz Hindi replies it drifted into
    Tamil, Kannada and Gujarati script even when the template pinned hi-IN.
    """
    capabilities = MODEL_CONFIGS.get(config.model)
    # Unknown model: fall back to the old family rule; pipecat validates it.
    takes_language = (
        capabilities.supports_language
        if capabilities
        else "saaras" not in config.model.lower()
    )
    takes_prompt = (
        capabilities.supports_prompt
        if capabilities
        else "saaras" in config.model.lower()
    )
    language_param = (
        get_sarvam_language(language_code=config.language_code)
        if takes_language
        else None
    )
    prompt_param = (config.prompt or None) if takes_prompt else None
    if config.language_code and not takes_language:
        logger.warning(
            f"Sarvam model {config.model} does not accept a language; "
            f"'{config.language_code}' ignored, the model auto-detects"
        )

    logger.info(
        f"Using Sarvam STT service with model={config.model}, language={'set' if language_param else 'none'}, prompt={'set' if prompt_param else 'none'}, vad_signals={config.vad_signals}, high_vad_sensitivity={config.high_vad_sensitivity}"
    )

    return SarvamSTTServiceWithInterruptionPolicy(
        self_interrupt=config.self_interrupt,
        api_key=config.api_key,
        model=config.model,
        sample_rate=config.sample_rate,
        settings=SarvamSTTService.Settings(
            language=language_param,
            prompt=prompt_param,
            vad_signals=config.vad_signals if config.vad_signals is not None else True,
            high_vad_sensitivity=(
                config.high_vad_sensitivity
                if config.high_vad_sensitivity is not None
                else False
            ),
        ),
    )
