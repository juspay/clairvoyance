from enum import Enum
from typing import Optional

from pipecat.transcriptions.language import Language

from app.core.logger import logger


class SarvamServiceType(str, Enum):
    """Enum to specify Sarvam service type for language handling."""

    TTS = "TTS"
    STT = "STT"


def get_sarvam_language(
    language_code: Optional[str],
    service_type: SarvamServiceType,
) -> Optional[Language]:
    """
    Convert SARVAM language code to Language enum with appropriate fallbacks.

    Args:
        language_code: Language code string (e.g., "en-IN", "hi-IN")
        service_type: Service type (TTS or STT) to determine fallback behavior

    Returns:
        - For TTS: Language enum value, fallback to EN_IN if invalid
        - For STT: Language enum value, or None if invalid/not provided
    """
    if language_code:
        try:
            return Language(language_code)
        except ValueError:
            # Different fallback behavior for TTS vs STT
            if service_type == SarvamServiceType.TTS:
                logger.warning(
                    f"Invalid TTS language code: {language_code}, falling back to EN_IN"
                )
                return Language.EN_IN
            else:
                logger.warning(
                    f"Invalid STT language code: {language_code}, returning None"
                )
                return None
    else:
        # No language code provided
        if service_type == SarvamServiceType.TTS:
            logger.warning(
                "No SARVAM TTS language code provided, falling back to EN_IN"
            )
            return Language.EN_IN
        else:
            logger.debug("No SARVAM STT language code provided, returning None")
            return None
