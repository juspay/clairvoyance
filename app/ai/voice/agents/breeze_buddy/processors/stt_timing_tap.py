"""STT timing tap: transcription-frame timestamps for the metrics collector.

The metrics collector sits downstream of the LLM/TTS, but the user aggregator
swallows Interim/TranscriptionFrames before they travel that far — so the
collector's STT-finalization measurement never fires in agent mode. This tap
sits between the transcription gate and the user aggregator (the last spot
where every interim and final transcript still flows), reports the events to
the collector, and passes the frames through untouched.
"""

from __future__ import annotations

from typing import Any

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

__all__ = ["STTTimingTapProcessor"]


class STTTimingTapProcessor(FrameProcessor):
    """Forwards transcription frames, timing them into the collector.

    Holds only a reference to the collector's note hooks — no queueing, no
    filtering, no ordering changes; a frame spends microseconds here.
    """

    def __init__(self, collector: Any, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._collector = collector

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, InterimTranscriptionFrame):
            self._collector.note_stt_interim()
        elif isinstance(frame, TranscriptionFrame):
            # InterimTranscriptionFrame is checked first, so this only
            # matches finalized transcripts.
            self._collector.note_stt_final()

        await self.push_frame(frame, direction)
