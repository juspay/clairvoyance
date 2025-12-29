"""
LLM Buffer-Based Streaming Optimization

Implements buffer-based streaming similar to Bolna's approach.
Yields text chunks to TTS as soon as buffer reaches threshold (40-60 chars).
This enables parallel LLM generation + TTS synthesis.

Expected latency reduction: 200-400ms
"""

import asyncio
from typing import AsyncIterator, Optional
from loguru import logger


class BufferedLLMStreamWrapper:
    """
    Wraps LLM streaming responses with buffer-based chunking.

    Instead of waiting for complete sentences or large chunks,
    this yields smaller chunks (40-60 chars) at word boundaries,
    enabling faster TTS synthesis start.
    """

    def __init__(
        self,
        buffer_size: int = 40,
        min_buffer_size: int = 20,
        enable_word_boundary: bool = True
    ):
        """
        Initialize buffer-based streaming wrapper.

        Args:
            buffer_size: Target buffer size before yielding (default: 40 chars)
            min_buffer_size: Minimum buffer size to consider yielding (default: 20 chars)
            enable_word_boundary: Yield at word boundaries (default: True)
        """
        self.buffer_size = buffer_size
        self.min_buffer_size = min_buffer_size
        self.enable_word_boundary = enable_word_boundary
        self.buffer = ""
        self.total_chars_yielded = 0

    async def stream_with_buffer(
        self,
        llm_stream: AsyncIterator[str],
        turn_id: Optional[str] = None
    ) -> AsyncIterator[str]:
        """
        Stream LLM responses with buffer-based chunking.

        Args:
            llm_stream: Async iterator of LLM text chunks
            turn_id: Optional turn ID for logging

        Yields:
            Text chunks at buffer boundaries
        """
        self.buffer = ""
        self.total_chars_yielded = 0
        first_chunk_yielded = False

        try:
            async for chunk in llm_stream:
                if not chunk:
                    continue

                self.buffer += chunk

                # Check if buffer is large enough to yield
                if len(self.buffer) >= self.buffer_size:
                    chunk_to_yield = self._extract_chunk_at_boundary()

                    if chunk_to_yield:
                        if not first_chunk_yielded:
                            logger.info(
                                f"[Turn {turn_id}] First LLM chunk yielded: {len(chunk_to_yield)} chars, "
                                f"total buffer processed: {len(self.buffer) + len(chunk_to_yield)} chars"
                            )
                            first_chunk_yielded = True

                        self.total_chars_yielded += len(chunk_to_yield)
                        yield chunk_to_yield

            # Yield remaining buffer
            if self.buffer.strip():
                logger.debug(
                    f"[Turn {turn_id}] Final LLM chunk yielded: {len(self.buffer)} chars, "
                    f"total yielded: {self.total_chars_yielded + len(self.buffer)} chars"
                )
                self.total_chars_yielded += len(self.buffer)
                yield self.buffer
                self.buffer = ""

        except asyncio.CancelledError:
            logger.info(f"[Turn {turn_id}] LLM streaming cancelled, buffer discarded")
            self.buffer = ""
            raise
        except Exception as e:
            logger.error(f"[Turn {turn_id}] Error in buffered LLM streaming: {e}")
            # Yield remaining buffer before raising
            if self.buffer.strip():
                yield self.buffer
                self.buffer = ""
            raise

    def _extract_chunk_at_boundary(self) -> Optional[str]:
        """
        Extract chunk from buffer at word boundary.

        Returns:
            Text chunk if boundary found, None otherwise
        """
        if not self.enable_word_boundary:
            # Just yield the buffer
            chunk = self.buffer[:self.buffer_size]
            self.buffer = self.buffer[self.buffer_size:]
            return chunk

        # Find last word boundary within buffer
        split_result = self.buffer.rsplit(" ", 1)

        if len(split_result) == 2:
            # Found a word boundary
            chunk_to_yield = split_result[0]
            self.buffer = split_result[1]

            # Only yield if chunk is substantial enough
            if len(chunk_to_yield) >= self.min_buffer_size:
                return chunk_to_yield

        # If buffer is getting too large, force yield even without perfect boundary
        if len(self.buffer) >= self.buffer_size * 2:
            chunk_to_yield = self.buffer[:self.buffer_size]
            self.buffer = self.buffer[self.buffer_size:]
            logger.debug(f"Force yielding chunk without word boundary: {len(chunk_to_yield)} chars")
            return chunk_to_yield

        return None


class LLMBufferConfig:
    """Configuration for LLM buffer-based streaming."""

    # Aggressive (lowest latency, may cut words)
    AGGRESSIVE = {
        "buffer_size": 30,
        "min_buffer_size": 15,
        "enable_word_boundary": True
    }

    # Balanced (good latency, preserves words)
    BALANCED = {
        "buffer_size": 40,
        "min_buffer_size": 20,
        "enable_word_boundary": True
    }

    # Conservative (higher quality, slightly more latency)
    CONSERVATIVE = {
        "buffer_size": 60,
        "min_buffer_size": 30,
        "enable_word_boundary": True
    }

    @classmethod
    def get_config(cls, mode: str = "balanced") -> dict:
        """
        Get buffer configuration by mode.

        Args:
            mode: One of "aggressive", "balanced", "conservative"

        Returns:
            Configuration dictionary
        """
        return getattr(cls, mode.upper(), cls.BALANCED)
