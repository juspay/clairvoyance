"""
Transcription Gate Processor

A single processor that controls which TranscriptionFrames reach the user
aggregator (and therefore the LLM). It provides two independent, composable
suppression modes:

1. Hard mute (VAD-independent)
   Activated via mute() / unmute() — typically called from a node pre-action
   when VAD is disabled. Drops ALL TranscriptionFrames unconditionally while
   muted, regardless of bot activity state.

2. Keyword filter (bot-active-aware)
   Configured at template level via KeywordFilterConfig. Drops TranscriptionFrames
   whose text matches configured keywords, but only while the bot is actively
   processing (LLM) or speaking (TTS). When the bot is idle, keyword-matching
   transcriptions pass through normally.

Both modes can be active simultaneously — either condition causes a drop.

Pipeline position (must be BEFORE ResponseStateGate):
    transport.input()
    → stt
    → TranscriptionGateProcessor   ← here
    → ResponseStateGate
    → user_aggregator (VAD / turn strategies)
    → llm
    ...

This processor is always inserted in the pipeline. When neither mode is active
it is a transparent passthrough with negligible overhead.
"""

import unicodedata
from typing import Optional

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
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


class TranscriptionGateProcessor(FrameProcessor):
    """Controls which TranscriptionFrames reach the LLM via two independent modes:
    hard mute and keyword filtering. Both modes drop frames silently — no frame
    is ever pushed downstream when either suppression condition is met.
    """

    def __init__(
        self,
        keyword_filter_config: Optional[KeywordFilterConfig] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        # --- Hard mute state ---
        self._muted: bool = False

        # --- Keyword filter state ---
        if keyword_filter_config and keyword_filter_config.enabled:
            self._keyword_filter_enabled = True
            self._match_type = keyword_filter_config.match_type
            self._keywords: list[str] = [
                norm
                for kw in keyword_filter_config.keywords
                if (norm := self._normalise(kw))
            ]
        else:
            self._keyword_filter_enabled = False
            self._match_type = KeywordMatchType.EXACT
            self._keywords = []

        # --- Bot activity tracking (for keyword filter) ---
        # Track LLM and TTS independently so the BOTH → LLM_PROCESSING transition
        # (TTS stops but LLM still running) keeps the filter active.
        self._llm_active: bool = False
        self._tts_active: bool = False

    # ------------------------------------------------------------------
    # Hard mute API
    # ------------------------------------------------------------------

    def mute(self) -> None:
        """Drop ALL TranscriptionFrames until unmute() is called."""
        self._muted = True
        logger.info("TranscriptionGate: hard mute engaged")

    def unmute(self) -> None:
        """Resume normal passthrough (keyword filter still applies if configured)."""
        self._muted = False
        logger.info("TranscriptionGate: hard mute released")

    @property
    def is_muted(self) -> bool:
        return self._muted

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

    def _keyword_matches(self, text: str) -> bool:
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

        # ---- Track bot activity state (for keyword filter) -----------
        if isinstance(frame, LLMFullResponseStartFrame):
            self._llm_active = True

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_active = False

        elif isinstance(frame, BotStartedSpeakingFrame):
            self._tts_active = True

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._tts_active = False

        # ---- Transcription suppression logic -------------------------
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
            # Mode 1: hard mute — drop unconditionally (both final and interim).
            # InterimTranscriptionFrame must also be dropped: TranscriptionUserTurnStartStrategy
            # with use_interim=True fires on interim frames, which triggers an interruption even
            # when the final TranscriptionFrame would have been suppressed.
            if self._muted:
                logger.debug(
                    f"TranscriptionGate: dropping {'interim ' if isinstance(frame, InterimTranscriptionFrame) else ''}"
                    f"transcription (hard mute active)"
                )
                return  # Silently drop

            # Mode 2: keyword filter — drop matching words while bot is active.
            # Only applies to final TranscriptionFrames; interim frames are too noisy to match reliably.
            if (
                isinstance(frame, TranscriptionFrame)
                and self._keyword_filter_enabled
                and self._is_bot_active()
                and self._keywords
                and self._keyword_matches(frame.text)
            ):
                logger.debug(
                    f"TranscriptionGate: dropping transcription "
                    f"(keyword match, match_type={self._match_type.value}, bot_active=True)"
                )
                return  # Silently drop

        # Pass everything else (and non-suppressed transcriptions) downstream
        await self.push_frame(frame, direction)
