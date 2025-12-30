"""
Hybrid text aggregator for Breeze Buddy.

This aggregator combines character-count buffering (like Bolna) with sentence-boundary
detection (like Pipecat default) to minimize latency while maintaining natural audio.

Key Features:
- Sends text to TTS after 40 characters (configurable) at natural break points
- Also sends on sentence boundaries (., !, ?)
- Safety timeout to prevent indefinite buffering
"""

from typing import AsyncIterator

from pipecat.utils.text.base_text_aggregator import (
    Aggregation,
    AggregationType,
    BaseTextAggregator,
)

from app.core.logger import logger

# Natural break points where we can split text without disrupting flow
NATURAL_BREAK_CHARS = {" ", ",", ";", ":", "-", "—"}
SENTENCE_ENDING_PUNCTUATION = {".", "!", "?"}


class HybridTextAggregator(BaseTextAggregator):
    """
    Hybrid text aggregator that combines character-count and sentence-boundary buffering.

    Triggers text output when:
    1. First chunk: Character count reaches first_chunk_min_chars AND we're at a natural break point
    2. Subsequent chunks: Character count reaches min_chars AND we're at a natural break point
    3. Sentence boundary is detected (., !, ?)
    4. Max characters reached (safety net)

    This provides ultra-low initial latency for the first chunk, then balanced latency/quality
    for subsequent chunks.
    """

    def __init__(
        self,
        min_chars: int = 40,
        max_chars: int = 200,
        enable_sentence_detection: bool = True,
        first_chunk_min_chars: int = 20,
    ):
        """
        Initialize the hybrid text aggregator.

        Args:
            min_chars: Minimum characters before considering a split (default: 40, matching Bolna)
            max_chars: Maximum characters before forcing a split (default: 200, safety net)
            enable_sentence_detection: Whether to also split on sentence boundaries (default: True)
            first_chunk_min_chars: Minimum characters for first chunk only (default: 20, faster initial response)
        """
        super().__init__()
        self._buffer = ""
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.enable_sentence_detection = enable_sentence_detection
        self.first_chunk_min_chars = first_chunk_min_chars
        self._is_first_chunk = True  # Track if this is the first chunk

        logger.info(
            f"Initialized HybridTextAggregator with first_chunk_min_chars={first_chunk_min_chars}, "
            f"min_chars={min_chars}, max_chars={max_chars}, enable_sentence_detection={enable_sentence_detection}"
        )

    @property
    def text(self) -> Aggregation:
        """Get the currently aggregated text."""
        return Aggregation(text=self._buffer, type=AggregationType.WORD)

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        """
        Aggregate text character-by-character and yield when conditions are met.

        Yields aggregations when:
        1. First chunk: Buffer >= first_chunk_min_chars AND current char is a natural break point
        2. Subsequent chunks: Buffer >= min_chars AND current char is a natural break point
        3. Sentence boundary detected (if enabled)
        4. Buffer >= max_chars (safety net)

        Args:
            text: The text to aggregate (can be single char or multiple chars)

        Yields:
            Aggregation objects containing completed text chunks
        """
        for char in text:
            self._buffer += char
            buffer_len = len(self._buffer)

            # Determine the threshold based on whether this is the first chunk
            current_min_chars = self.first_chunk_min_chars if self._is_first_chunk else self.min_chars

            # TRIGGER 1: Max characters reached (safety net)
            if buffer_len >= self.max_chars:
                # Force split at last natural break point
                result = await self._yield_at_break_point(force=True)
                if result.text:  # Only mark as sent if we actually yielded something
                    self._is_first_chunk = False
                yield result
                continue

            # TRIGGER 2: Sentence boundary (if enabled)
            if self.enable_sentence_detection and char in SENTENCE_ENDING_PUNCTUATION:
                if buffer_len > 0:  # Only yield if we have content
                    aggregation = Aggregation(
                        text=self._buffer.strip(),
                        type=AggregationType.SENTENCE
                    )
                    chunk_type = "first chunk (sentence)" if self._is_first_chunk else "sentence"
                    logger.debug(
                        f"Yielding {chunk_type}: '{aggregation.text}' ({len(aggregation.text)} chars)"
                    )
                    self._buffer = ""
                    self._is_first_chunk = False
                    yield aggregation
                continue

            # TRIGGER 3: Character threshold + natural break point
            # Use first_chunk_min_chars for first chunk, min_chars for subsequent chunks
            if buffer_len >= current_min_chars and char in NATURAL_BREAK_CHARS:
                aggregation = Aggregation(
                    text=self._buffer.strip(),
                    type=AggregationType.WORD
                )
                chunk_type = f"first chunk ({current_min_chars} chars)" if self._is_first_chunk else f"chunk ({current_min_chars} chars)"
                logger.debug(
                    f"Yielding {chunk_type}: '{aggregation.text}' ({len(aggregation.text)} chars)"
                )
                self._buffer = ""
                self._is_first_chunk = False
                yield aggregation

    async def _yield_at_break_point(self, force: bool = False) -> Aggregation:
        """
        Yield text at the last natural break point.

        Args:
            force: If True, yield entire buffer if no break point found

        Returns:
            Aggregation object
        """
        # Try to find last natural break point
        for i in range(len(self._buffer) - 1, -1, -1):
            if self._buffer[i] in NATURAL_BREAK_CHARS:
                # Split at break point
                text_to_yield = self._buffer[:i+1].strip()
                self._buffer = self._buffer[i+1:]

                logger.debug(
                    f"Yielding at break point: '{text_to_yield}' ({len(text_to_yield)} chars), "
                    f"remaining: '{self._buffer}' ({len(self._buffer)} chars)"
                )

                return Aggregation(
                    text=text_to_yield,
                    type=AggregationType.WORD
                )

        # No break point found
        if force and self._buffer:
            # Yield entire buffer
            aggregation = Aggregation(
                text=self._buffer.strip(),
                type=AggregationType.WORD
            )
            logger.warning(
                f"Force yielding without break point: '{aggregation.text}' ({len(aggregation.text)} chars)"
            )
            self._buffer = ""
            return aggregation

        # Return empty aggregation if nothing to yield
        return Aggregation(text="", type=AggregationType.WORD)

    async def flush(self) -> Aggregation | None:
        """
        Flush any pending text in the buffer.

        Called at the end of LLM response (LLMFullResponseEndFrame).

        Returns:
            Aggregation with remaining text, or None if buffer is empty
        """
        if self._buffer.strip():
            aggregation = Aggregation(
                text=self._buffer.strip(),
                type=AggregationType.SENTENCE  # Mark as sentence for final flush
            )
            logger.debug(
                f"Flushing remaining buffer: '{aggregation.text}' ({len(aggregation.text)} chars)"
            )
            self._buffer = ""
            return aggregation
        return None

    async def handle_interruption(self):
        """
        Handle interruptions by clearing the buffer.

        When user interrupts, discard any pending text and reset first chunk flag.
        """
        if self._buffer:
            logger.info(
                f"Handling interruption: discarding buffer '{self._buffer}' ({len(self._buffer)} chars)"
            )
            self._buffer = ""
        self._is_first_chunk = True  # Reset for next LLM response

    async def reset(self):
        """Reset the aggregator to initial state."""
        logger.debug("Resetting HybridTextAggregator")
        self._buffer = ""
        self._is_first_chunk = True  # Reset for next LLM response


class CharacterCountOnlyAggregator(BaseTextAggregator):
    """
    Simple character-count aggregator (exactly like Bolna's 40-char buffering).

    This is the simplest approach - just count characters and yield at break points.
    No sentence detection, minimal complexity.
    """

    def __init__(self, buffer_size: int = 40):
        """
        Initialize character-count aggregator.

        Args:
            buffer_size: Number of characters to accumulate before yielding (default: 40)
        """
        super().__init__()
        self._buffer = ""
        self.buffer_size = buffer_size
        logger.info(f"Initialized CharacterCountOnlyAggregator with buffer_size={buffer_size}")

    @property
    def text(self) -> Aggregation:
        """Get the currently aggregated text."""
        return Aggregation(text=self._buffer, type=AggregationType.WORD)

    async def aggregate(self, text: str) -> AsyncIterator[Aggregation]:
        """
        Aggregate text and yield every buffer_size characters at natural break points.

        Args:
            text: The text to aggregate

        Yields:
            Aggregation objects when buffer reaches size threshold at break point
        """
        for char in text:
            self._buffer += char

            # Yield when we reach buffer_size AND current char is a natural break
            if len(self._buffer) >= self.buffer_size and char in NATURAL_BREAK_CHARS:
                aggregation = Aggregation(
                    text=self._buffer.strip(),
                    type=AggregationType.WORD
                )
                logger.debug(
                    f"Yielding chunk: '{aggregation.text}' ({len(aggregation.text)} chars)"
                )
                self._buffer = ""
                yield aggregation

    async def flush(self) -> Aggregation | None:
        """Flush remaining buffer."""
        if self._buffer.strip():
            aggregation = Aggregation(
                text=self._buffer.strip(),
                type=AggregationType.WORD
            )
            logger.debug(f"Flushing: '{aggregation.text}' ({len(aggregation.text)} chars)")
            self._buffer = ""
            return aggregation
        return None

    async def handle_interruption(self):
        """Clear buffer on interruption."""
        if self._buffer:
            logger.info(f"Interruption: discarding '{self._buffer}'")
            self._buffer = ""

    async def reset(self):
        """Reset to initial state."""
        logger.debug("Resetting CharacterCountOnlyAggregator")
        self._buffer = ""
