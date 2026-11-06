"""Transcript collector for stream-mode pipelines (no LLM context)."""

from typing import Any, Dict, List

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor


class TranscriptCollectorProcessor(FrameProcessor):
    """Collects user transcriptions and bot TTS text for DB storage.

    In stream mode there is no LLMContext to pull messages from at
    end-of-conversation.  This processor sits in the pipeline,
    observes TranscriptionFrame (user speech) and TTSSpeakFrame
    (client-driven TTS), and stores them as [{role, content}].
    All frames pass through unchanged.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._messages: List[Dict[str, Any]] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # Only collect in downstream direction to avoid any risk of double-counting
        # if a frame is ever replayed upstream. TranscriptionFrame is final-only
        # (InterimTranscriptionFrame is a separate class and is ignored).
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TranscriptionFrame) and frame.text:
                self._messages.append({"role": "user", "content": frame.text})
            elif isinstance(frame, TTSSpeakFrame) and frame.text:
                self._messages.append({"role": "assistant", "content": frame.text})

        await self.push_frame(frame, direction)

    def get_transcription(self) -> List[Dict[str, Any]]:
        return list(self._messages)
