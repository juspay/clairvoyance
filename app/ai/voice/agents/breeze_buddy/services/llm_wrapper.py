"""
Breeze Buddy LLM Service Wrapper with Buffer Streaming

Provides buffer-based streaming for LLM responses to enable parallel
LLM generation + TTS synthesis, reducing end-to-end latency.
"""

from typing import AsyncIterator
from loguru import logger
from pipecat.services.azure import AzureLLMService

from app.ai.voice.agents.breeze_buddy.utils.llm_buffer_streaming import (
    BufferedLLMStreamWrapper,
    LLMBufferConfig
)
from app.core.config.static import (
    ENABLE_BREEZE_BUDDY_LLM_BUFFER_STREAMING,
    BREEZE_BUDDY_LLM_BUFFER_SIZE
)


class BreezeBuddyLLMWrapper(AzureLLMService):
    """LLM service with optional buffer-based streaming."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.enable_buffer_streaming = ENABLE_BREEZE_BUDDY_LLM_BUFFER_STREAMING

        if self.enable_buffer_streaming:
            config = LLMBufferConfig.get_config("balanced")
            config["buffer_size"] = BREEZE_BUDDY_LLM_BUFFER_SIZE
            self.buffer_wrapper = BufferedLLMStreamWrapper(**config)
            logger.info(f"🚀 LLM buffer streaming enabled ({BREEZE_BUDDY_LLM_BUFFER_SIZE}-char chunks)")
        else:
            self.buffer_wrapper = None

    async def _stream_chat_completions(self, context) -> AsyncIterator[str]:
        """Override to add buffer-based streaming."""
        base_stream = super()._stream_chat_completions(context)

        if self.buffer_wrapper:
            turn_id = getattr(context, 'turn_id', 'unknown')
            async for chunk in self.buffer_wrapper.stream_with_buffer(base_stream, turn_id):
                yield chunk
        else:
            async for chunk in base_stream:
                yield chunk
