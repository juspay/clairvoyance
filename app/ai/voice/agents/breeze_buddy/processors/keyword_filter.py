"""
Busy State Keyword Filter Processor

Filters configured keywords from user transcriptions when the bot is busy
(speaking, processing LLM response, or executing function calls).

Allows keywords to pass through when bot is idle (e.g., initial "hello" greeting),
but filters them when bot is processing to prevent false triggers during delays.
"""

import re
from typing import Optional, Set

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)

try:
    from pipecat.frames.frames import (
        FunctionCallResultFrame,
        FunctionCallsStartedFrame,
    )

    FUNCTION_CALL_FRAMES_AVAILABLE = True
except ImportError:
    FUNCTION_CALL_FRAMES_AVAILABLE = False

from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class BusyStateKeywordFilter(FrameProcessor):
    """
    Filters configured keywords from transcriptions when bot is busy.

    Tracks bot state (speaking, LLM processing, function calls) and filters
    configured keywords only when bot is busy. Allows keywords through when
    bot is idle.

    Configuration:
        - keywords: List of keywords to filter (e.g., ["hello", "hey"])
        - match_mode: "exact" or "contains" (default: "exact")
        - case_sensitive: Whether to match case-sensitively (default: False)
        - remove_punctuation: Whether to remove punctuation before matching (default: True)
    """

    def __init__(
        self,
        keywords: Optional[list[str]] = None,
        match_mode: str = "exact",
        case_sensitive: bool = False,
        remove_punctuation: bool = True,
        **kwargs,
    ):
        """Initialize the keyword filter.

        Args:
            keywords: List of keywords to filter when bot is busy
            match_mode: "exact" for exact word match, "contains" for substring match
            case_sensitive: Whether matching should be case-sensitive
            remove_punctuation: Whether to remove punctuation before matching
        """
        super().__init__(**kwargs)

        # Configuration
        self._keywords: Set[str] = set(keywords or [])
        self._match_mode = match_mode
        self._case_sensitive = case_sensitive
        self._remove_punctuation = remove_punctuation

        # Normalize keywords for matching
        self._normalized_keywords: Set[str] = set()
        for keyword in self._keywords:
            normalized = self._normalize_text(keyword)
            self._normalized_keywords.add(normalized)

        # State tracking
        self._bot_speaking = False
        self._llm_processing = False
        self._function_calls_active = False

        logger.info(
            f"BusyStateKeywordFilter initialized: keywords={self._keywords}, "
            f"match_mode={match_mode}, case_sensitive={case_sensitive}"
        )

    @property
    def is_bot_busy(self) -> bool:
        """Check if bot is currently busy (speaking, processing, or executing functions)."""
        return self._bot_speaking or self._llm_processing or self._function_calls_active

    def _normalize_text(self, text: str) -> str:
        """Normalize text for matching based on configuration.

        Args:
            text: Text to normalize

        Returns:
            Normalized text
        """
        normalized = text

        if self._remove_punctuation:
            # Remove all punctuation
            normalized = re.sub(r"[^\w\s]", "", normalized)

        if not self._case_sensitive:
            normalized = normalized.lower()

        return normalized.strip()

    def _should_filter_transcription(self, text: str) -> bool:
        """Check if transcription should be filtered based on keywords.

        Args:
            text: Transcription text to check

        Returns:
            True if text should be filtered, False otherwise
        """
        if not self._keywords or not text:
            return False

        normalized_text = self._normalize_text(text)

        if self._match_mode == "exact":
            # Split into words and check for exact match
            words = normalized_text.split()
            for word in words:
                if word in self._normalized_keywords:
                    return True
            return False

        elif self._match_mode == "contains":
            # Check if any keyword is a substring
            for keyword in self._normalized_keywords:
                if keyword in normalized_text:
                    return True
            return False

        return False

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames to track bot state and filter keywords.

        Args:
            frame: Frame to process
            direction: Frame direction
        """
        await super().process_frame(frame, direction)

        # Track bot speaking state
        if isinstance(frame, BotStartedSpeakingFrame):
            self._bot_speaking = True
            logger.debug("BusyStateKeywordFilter: Bot started speaking")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._bot_speaking = False
            logger.debug("BusyStateKeywordFilter: Bot stopped speaking")

        # Track LLM processing state
        elif isinstance(frame, LLMFullResponseStartFrame):
            self._llm_processing = True
            logger.debug("BusyStateKeywordFilter: LLM processing started")

        elif isinstance(frame, LLMFullResponseEndFrame):
            self._llm_processing = False
            logger.debug("BusyStateKeywordFilter: LLM processing ended")

        # Track function call state (if available)
        elif FUNCTION_CALL_FRAMES_AVAILABLE:
            if isinstance(frame, FunctionCallsStartedFrame):
                self._function_calls_active = True
                logger.debug("BusyStateKeywordFilter: Function calls started")

            elif isinstance(frame, FunctionCallResultFrame):
                self._function_calls_active = False
                logger.debug("BusyStateKeywordFilter: Function calls ended")

        # Filter transcriptions when bot is busy
        elif isinstance(frame, TranscriptionFrame):
            # Only filter final transcriptions (not interim)
            if not frame.is_interim:  # type: ignore
                should_filter = self._should_filter_transcription(frame.text)

                if should_filter and self.is_bot_busy:
                    # Bot is busy and text contains filtered keyword - drop the frame
                    logger.info(
                        f"BusyStateKeywordFilter: Filtered transcription '{frame.text}' "
                        f"(bot_speaking={self._bot_speaking}, "
                        f"llm_processing={self._llm_processing}, "
                        f"function_calls={self._function_calls_active})"
                    )
                    return  # Don't push frame - it's filtered

                elif should_filter and not self.is_bot_busy:
                    # Keyword detected but bot is idle - allow it through
                    logger.debug(
                        f"BusyStateKeywordFilter: Allowing transcription '{frame.text}' "
                        f"(bot is idle)"
                    )

        # Pass all other frames through (including interim transcriptions)
        await self.push_frame(frame, direction)
