"""
Keyword Filter Processor

Intercepts TranscriptionFrames while the bot is active (LLM processing or TTS
speaking) and silently drops them when the transcribed text matches any of the
configured keywords.

This prevents two things:
 1. The filtered transcription from reaching the LLM context.
 2. The filtered transcription from triggering a user interruption.

The processor must be placed BEFORE the ResponseStateGate in the pipeline so that
matching frames are consumed before the gate's interruption logic sees them.

Pipeline position:
    transport.input()
    → stt
    → KeywordFilterProcessor   ← here
    → ResponseStateGate
    → user_aggregator (VAD / turn strategies)
    → llm
    ...
"""

import unicodedata

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.template.types import (
    KeywordFilterConfig,
    KeywordMatchType,
)
from app.core.logger import logger


class KeywordFilterProcessor(FrameProcessor):
    """Drop transcription frames whose text matches configured keywords while
    the bot is actively processing or speaking.

    When the bot is IDLE (neither LLM processing nor TTS speaking) every
    transcription passes through normally — the filter only engages during
    bot activity to suppress accidental speech like a background "hello" or
    acknowledgement words that would otherwise cancel the bot's response.
    """

    def __init__(self, config: KeywordFilterConfig, **kwargs):
        super().__init__(**kwargs)
        self._enabled = config.enabled
        self._match_type = config.match_type
        # Normalise keywords once at construction time for fast matching.
        # Uses the same _normalise pipeline as incoming transcriptions so both
        # sides are always comparable.
        self._keywords: list[str] = [
            norm for kw in config.keywords if (norm := self._normalise(kw))
        ]

        # Track LLM and TTS activity separately — mirrors ResponseStateGate's
        # full state machine so we stay active during BOTH → LLM_PROCESSING
        # transitions (i.e. TTS stops but LLM is still running).
        self._llm_active: bool = False
        self._tts_active: bool = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _is_bot_active(self) -> bool:
        return self._llm_active or self._tts_active

    @staticmethod
    def _normalise(text: str) -> str:
        """Strip surrounding whitespace and remove punctuation/symbols, preserving
        Unicode letters, digits, and combining marks so multilingual keywords
        (Indic, Arabic, etc.) round-trip correctly without vowel-sign collisions."""
        cleaned = "".join(
            c
            for c in text.strip().lower()
            if unicodedata.category(c)[0] not in ("P", "S")
        )
        return cleaned.strip()

    def _matches(self, text: str) -> bool:
        """Return True if *text* matches any configured keyword."""
        normalised = self._normalise(text)
        if self._match_type == KeywordMatchType.EXACT:
            return normalised in self._keywords
        # INCLUDES
        return any(kw in normalised for kw in self._keywords)

    # ------------------------------------------------------------------
    # FrameProcessor implementation
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # ---- Track bot activity state --------------------------------
        # Track LLM and TTS independently so that the BOTH → LLM_PROCESSING
        # transition (TTS stops but LLM still running) keeps the filter active.
        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_active = True

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_active = False

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._tts_active = True

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._tts_active = False

        # ---- Keyword filtering ---------------------------------------
        elif isinstance(frame, TranscriptionFrame):
            if self._enabled and self._is_bot_active() and self._keywords:
                if self._matches(frame.text):
                    logger.info(
                        f"KeywordFilter: dropping transcription '{frame.text}' "
                        f"(match_type={self._match_type.value}, bot_active=True)"
                    )
                    return  # Silently drop — do NOT call push_frame

        # Pass everything else (and non-matching transcriptions) downstream
        await self.push_frame(frame, direction)
