"""Sarvam TTS helpers and builder with automatic language detection."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Optional

from pipecat.frames.frames import Frame
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.utils.text.base_text_aggregator import BaseTextAggregator

from app.ai.voice.agents.breeze_buddy.utils.hybrid_text_aggregator import (
    HybridTextAggregator,
    CharacterCountOnlyAggregator,
)
from app.core.logger import logger

__all__ = [
    "SarvamTTSConfig",
    "get_sarvam_language",
    "build_sarvam_tts",
    "LanguageAwareSarvamTTS",
]


# Unicode ranges for Indian scripts
SCRIPT_RANGES = {
    "telugu": (0x0C00, 0x0C7F),
    "devanagari": (0x0900, 0x097F),  # Hindi, Marathi, Sanskrit
    "tamil": (0x0B80, 0x0BFF),
    "kannada": (0x0C80, 0x0CFF),
    "malayalam": (0x0D00, 0x0D7F),
    "bengali": (0x0980, 0x09FF),
    "gujarati": (0x0A80, 0x0AFF),
    "punjabi": (0x0A00, 0x0A7F),  # Gurmukhi
    "odia": (0x0B00, 0x0B7F),
}

# Map script to Sarvam language code
SCRIPT_TO_SARVAM_LANG = {
    "telugu": "te-IN",
    "devanagari": "hi-IN",
    "tamil": "ta-IN",
    "kannada": "kn-IN",
    "malayalam": "ml-IN",
    "bengali": "bn-IN",
    "gujarati": "gu-IN",
    "punjabi": "pa-IN",
    "odia": "od-IN",
    "english": "en-IN",
}


def detect_script(text: str) -> str:
    """Detect the dominant script in the text.

    Returns 'english' as the default fallback if any error occurs.
    """
    try:
        script_counts = {script: 0 for script in SCRIPT_RANGES}
        script_counts["english"] = 0

        for char in text:
            code_point = ord(char)
            for script, (start, end) in SCRIPT_RANGES.items():
                if start <= code_point <= end:
                    script_counts[script] += 1
                    break
            else:
                if 0x0041 <= code_point <= 0x005A or 0x0061 <= code_point <= 0x007A:
                    script_counts["english"] += 1

        main_script = "english"
        max_count = 0
        for script, count in script_counts.items():
            if count > max_count:
                max_count = count
                main_script = script

        return main_script
    except Exception as e:
        logger.warning(f"[SARVAM] Error detecting script: {e}, defaulting to english")
        return "english"


@dataclass
class SarvamTTSConfig:
    """Configuration for Sarvam TTS."""

    api_key: str
    voice_id: str
    model: str
    pitch: float
    pace: float
    language_code: Optional[str] = None
    enable_preprocessing: bool = True
    # NEW: Text aggregation configuration
    use_hybrid_aggregator: bool = True  # Enable 40-char + sentence buffering
    min_chars: int = 40  # Minimum characters before sending to TTS (subsequent chunks)
    max_chars: int = 200  # Maximum characters (safety net)
    enable_sentence_detection: bool = True  # Also split on sentence boundaries
    first_chunk_min_chars: int = 20  # Minimum characters for first chunk only (ultra-low latency)


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
    """Create a language-aware Sarvam TTS service with auto-detection and custom text aggregation.

    By default, uses HybridTextAggregator which combines:
    - 40-character buffering (like Bolna) for low latency
    - Sentence-boundary detection (like default Pipecat) for natural audio

    This reduces latency from ~2.0-2.5s to ~1.0-1.5s while maintaining quality.
    """

    language = get_sarvam_language(config.language_code)

    logger.info(
        f"Using LanguageAwareSarvamTTS with model={config.model}, voice_id={config.voice_id}, "
        f"language={language}, pitch={config.pitch}, pace={config.pace}, "
        f"enable_preprocessing={config.enable_preprocessing}"
    )

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

    return LanguageAwareSarvamTTS(
        api_key=config.api_key,
        voice_id=config.voice_id,
        model=config.model,
        params=SarvamTTSService.InputParams(
            language=language,
            pitch=config.pitch,
            pace=config.pace,
            enable_preprocessing=config.enable_preprocessing,
        ),
        text_aggregator=text_aggregator,  # 🔥 THIS IS THE KEY CHANGE!
    )


class LanguageAwareSarvamTTS(SarvamTTSService):
    """
    Sarvam TTS with automatic script detection and language switching.

    Detects the script (Telugu, Hindi, Tamil, etc.) from the LLM output text
    and switches the TTS language code before generating speech.
    """

    async def _switch_language_if_needed(self, text: str) -> bool:
        """Detect script and switch language if different from current.

        Returns False on any error to ensure TTS continues with current language.
        """
        try:
            detected_script = detect_script(text)
            new_lang_code = SCRIPT_TO_SARVAM_LANG.get(detected_script, "en-IN")
            current_lang_code = self._settings.get("target_language_code", "en-IN")

            if new_lang_code != current_lang_code:
                logger.info(
                    f"[SARVAM] Script detected: {detected_script} - switching {current_lang_code} to {new_lang_code}"
                )
                self._settings["target_language_code"] = new_lang_code

                try:
                    await self._send_config()
                    logger.info(f"[SARVAM] Language switched to {new_lang_code}")
                    return True
                except Exception as e:
                    logger.warning(
                        f"[SARVAM] Failed to send config for language switch: {e}"
                    )
                    return False

            return False
        except Exception as e:
            logger.warning(f"[SARVAM] Error in language switching: {e}")
            return False

    async def run_tts(self, text: str) -> AsyncGenerator[Frame, None]:
        """Override to auto-detect language before TTS generation.

        Ensures TTS always proceeds even if language detection fails.
        """
        try:
            await self._switch_language_if_needed(text)
        except Exception as e:
            logger.warning(
                f"[SARVAM] Language switch failed, continuing with current language: {e}"
            )

        async for frame in super().run_tts(text):
            yield frame
