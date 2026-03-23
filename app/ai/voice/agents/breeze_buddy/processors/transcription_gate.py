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

import asyncio
import time
import unicodedata
from typing import Any, Dict, List, Optional

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

    Also captures user transcriptions in real-time for recovery on abrupt disconnect.
    Assistant transcriptions are captured separately by AssistantTranscriptionObserver
    which writes to the same shared transcription storage.
    """

    def __init__(
        self,
        keyword_filter_config: Optional[KeywordFilterConfig] = None,
        **kwargs,
    ):
        super().__init__(**kwargs)

        # --- Hard mute state ---
        self._muted: bool = False
        self._timed_unmute_task: Optional[asyncio.Task] = None

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

        # --- Real-time transcription collection ---
        # Captures user transcriptions as they flow through for recovery on abrupt disconnect.
        # Each entry: {"role": "user"|"assistant", "content": str, "timestamp": float}
        # Assistant transcriptions are added by AssistantTranscriptionObserver to the same list.
        self._real_time_transcriptions: List[Dict[str, Any]] = []
        # Track pending interim so we capture partial speech if call drops mid-utterance
        self._pending_user_interim: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------
    # Hard mute API
    # ------------------------------------------------------------------

    def mute(self) -> None:
        """Drop ALL TranscriptionFrames until unmute() is called."""
        self._cancel_timed_unmute()
        self._muted = True
        logger.info("TranscriptionGate: hard mute engaged")

    def unmute(self) -> None:
        """Resume normal passthrough (keyword filter still applies if configured)."""
        self._cancel_timed_unmute()
        self._muted = False
        logger.info("TranscriptionGate: hard mute released")

    def mute_for(self, duration: float) -> None:
        """Drop ALL TranscriptionFrames for *duration* seconds, then auto-unmute.

        Any previous timed-unmute task is cancelled before scheduling a new one.
        An explicit call to unmute() (or another mute/mute_for) also cancels the
        pending timer.
        """
        self._cancel_timed_unmute()
        self._muted = True
        self._timed_unmute_task = asyncio.create_task(self._auto_unmute(duration))
        logger.info(f"TranscriptionGate: hard mute engaged for {duration}s")

    def _cancel_timed_unmute(self) -> None:
        """Cancel any pending timed-unmute task."""
        if self._timed_unmute_task and not self._timed_unmute_task.done():
            self._timed_unmute_task.cancel()
            logger.debug("TranscriptionGate: cancelled pending timed unmute")
        self._timed_unmute_task = None

    async def _auto_unmute(self, duration: float) -> None:
        """Sleep for *duration* seconds then release the hard mute."""
        try:
            await asyncio.sleep(duration)
            self._muted = False
            self._timed_unmute_task = None
            logger.info(f"TranscriptionGate: hard mute auto-released after {duration}s")
        except asyncio.CancelledError:
            pass

    @property
    def is_muted(self) -> bool:
        return self._muted

    # ------------------------------------------------------------------
    # Real-time transcription collection API
    # ------------------------------------------------------------------

    @property
    def collected_transcriptions(self) -> List[Dict[str, Any]]:
        """Get all transcriptions collected in real-time.

        Returns a list of dicts with keys: role, content, timestamp.
        This includes both user transcriptions (from STT) and assistant responses
        (from LLM output, not post-TTS — may differ from actual spoken audio).
        """
        return self._real_time_transcriptions.copy()

    @property
    def transcription_storage(self) -> List[Dict[str, Any]]:
        """Get the shared transcription storage list.

        Returns the actual list (not a copy) for use by AssistantTranscriptionObserver
        to append assistant transcriptions to the same storage.
        """
        return self._real_time_transcriptions

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
            # Note: Assistant transcriptions are captured by AssistantTranscriptionObserver
            # which writes to the same shared transcription storage.

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
            # Applies to both final TranscriptionFrames and InterimTranscriptionFrames
            if (
                self._keyword_filter_enabled
                and self._is_bot_active()
                and self._keywords
                and self._keyword_matches(frame.text)
            ):
                logger.debug(
                    f"TranscriptionGate: dropping transcription "
                    f"(keyword match, match_type={self._match_type.value}, bot_active=True)"
                )
                return  # Silently drop

            # ---- Real-time transcription capture ----
            # Track interim transcriptions as pending so we capture partial speech
            # if the call drops mid-utterance (before STT emits a final frame).
            if isinstance(frame, InterimTranscriptionFrame) and frame.text.strip():
                entry = {
                    "role": "user",
                    "content": frame.text.strip(),
                    "timestamp": time.time(),
                    "interim": True,
                }
                if self._pending_user_interim is None:
                    # First interim for this utterance - append to list
                    self._real_time_transcriptions.append(entry)
                    self._pending_user_interim = entry
                else:
                    # Update existing pending entry in-place
                    self._pending_user_interim["content"] = frame.text.strip()
                    self._pending_user_interim["timestamp"] = time.time()

            elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
                # Final frame - finalize the pending interim or append new entry
                if self._pending_user_interim is not None:
                    # Replace interim content with final
                    self._pending_user_interim["content"] = frame.text.strip()
                    self._pending_user_interim["timestamp"] = time.time()
                    self._pending_user_interim.pop("interim", None)
                    self._pending_user_interim = None
                else:
                    # No pending interim - append new final entry
                    self._real_time_transcriptions.append(
                        {
                            "role": "user",
                            "content": frame.text.strip(),
                            "timestamp": time.time(),
                        }
                    )
                logger.debug(
                    f"TranscriptionGate: captured user transcription "
                    f"({len(frame.text)} chars)"
                )

        # Pass everything else (and non-suppressed transcriptions) downstream
        await self.push_frame(frame, direction)
