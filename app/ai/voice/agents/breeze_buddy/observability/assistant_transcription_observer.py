"""
Assistant Transcription Observer

Captures LLM-generated text (assistant responses) as they flow through the pipeline.
This observer works in conjunction with TranscriptionGateProcessor to capture both
user and assistant transcriptions in real-time.

The observer captures LLMTextFrame events from the LLM service and accumulates
them into complete assistant turns, storing them in the provided transcription list.
"""

import time
from typing import Any, Dict, List

from pipecat.frames.frames import (
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.services.llm_service import LLMService

from app.core.logger import logger


class AssistantTranscriptionObserver(BaseObserver):
    """Observer that captures assistant (LLM) text and stores it for transcription.

    This observer should be registered with the PipelineTask to capture LLMTextFrame
    events from the LLM service. It accumulates text tokens and stores complete
    assistant turns in a shared transcription list.

    Args:
        transcription_storage: A shared list where transcriptions will be stored.
                             Each entry is a dict with keys: role, content, timestamp.
    """

    def __init__(self, transcription_storage: List[Dict[str, Any]]):
        super().__init__()
        self._transcription_storage = transcription_storage
        self._pending_assistant_text: str = ""
        self._is_generating: bool = False

    async def on_push_frame(self, data: FramePushed):
        """Handle frame push events and capture LLM-generated text.

        Args:
            data: The frame push event data containing source, destination,
                  frame, direction, and timestamp information.
        """
        src = data.source
        frame = data.frame

        # Only process frames from LLM service
        if not isinstance(src, LLMService):
            return

        # Track when LLM starts generating
        if isinstance(frame, LLMFullResponseStartFrame):
            self._is_generating = True
            self._pending_assistant_text = ""

        # Accumulate text tokens
        elif isinstance(frame, LLMTextFrame):
            if self._is_generating and frame.text:
                self._pending_assistant_text += frame.text

        # Store complete assistant turn when LLM finishes
        elif isinstance(frame, LLMFullResponseEndFrame):
            self._is_generating = False
            if self._pending_assistant_text.strip():
                self._transcription_storage.append(
                    {
                        "role": "assistant",
                        "content": self._pending_assistant_text.strip(),
                        "timestamp": time.time(),
                    }
                )
                logger.debug(
                    f"AssistantTranscriptionObserver: captured assistant turn "
                    f"({len(self._pending_assistant_text)} chars)"
                )
                self._pending_assistant_text = ""
