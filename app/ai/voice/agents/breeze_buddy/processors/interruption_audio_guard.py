"""Interruption audio guard: drop stale TTS audio that races the flush.

On a barge-in, pipecat's websocket sender writes the telephony clear event
(Plivo ``clearAudio``) when the ``InterruptionFrame`` passes through — but the
sender's audio task may concurrently be mid-write of an already-dequeued PCM
chunk of the interrupted reply. That ``playAudio`` lands on the socket AFTER
the clear, so the carrier replays a fragment of the OLD reply once the NEW
reply starts playing (heard as an "echo" of the interrupted line; observed on
the 2026-09-06 15:45 redbus call).

The fix is a guard between the TTS service and the output transport: after an
``InterruptionFrame``, drop every audio frame until the next
``TTSStartedFrame``. Any audio in that window belongs to a context that was
interrupted — legitimate audio can only follow a NEW utterance, which always
begins with ``TTSStartedFrame`` (TTSService ``push_start_frame=True``; the
early-fire path's ``TTSSpeakFrame`` creates a fresh audio context per
utterance, so a started frame precedes every say block). The
``TTSSpeakFrame`` itself is consumed inside the TTS service and never flows
downstream, so TTSStartedFrame is the earliest downstream-visible signal of a
fresh utterance.

A max-armed timeout (defensive — no new utterance should ever take this long)
guarantees the guard can never stay armed forever if that frame is lost.

Pipeline position:
    ... → llm → prose_guard → tts → InterruptionAudioGuardProcessor →
    metrics → output
"""

from __future__ import annotations

import time

from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    OutputAudioRawFrame,
    TTSAudioRawFrame,
    TTSStartedFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger

__all__ = ["InterruptionAudioGuardProcessor"]

# Defensive ceiling on how long the guard stays armed without a fresh
# TTSStartedFrame. An interrupted → new-utterance gap is bounded by the user
# finishing their speech plus one LLM round trip (<2s even in the worst
# case); anything longer means the disarm frame was lost.
_MAX_ARMED_SECS = 2.0


class InterruptionAudioGuardProcessor(FrameProcessor):
    """Drops TTS audio frames between an interruption and the next utterance."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._armed: bool = False
        self._armed_at: float = 0.0
        self._dropped: int = 0

    @property
    def armed(self) -> bool:
        return self._armed

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        # The base class owns lifecycle: StartFrame creates this processor's
        # non-system frame task, InterruptionFrame flushes its queue,
        # Cancel/pause/resume frames manage processing. Without this call no
        # task ever consumes the guard's process queue — every audio frame
        # enqueues and is never forwarded, muting the line completely while
        # everything upstream looks healthy (the 2026-09-06 16:16 silent
        # call: TTS synthesis + service metrics logged, zero audio played,
        # zero [TURN METRICS]).
        await super().process_frame(frame, direction)

        if isinstance(frame, InterruptionFrame):
            self._armed = True
            self._armed_at = time.monotonic()
            self._dropped = 0
            logger.info(
                f"{self}: armed — dropping stale TTS audio until next utterance"
            )
        elif isinstance(frame, TTSStartedFrame) and self._armed:
            self._disarm("new utterance started")
        elif isinstance(frame, (TTSAudioRawFrame, OutputAudioRawFrame)):
            if self._armed and (time.monotonic() - self._armed_at) <= _MAX_ARMED_SECS:
                self._dropped += 1
                if self._dropped == 1:
                    logger.info(
                        f"{self}: dropping stale TTS audio after interruption "
                        f"(until next TTSStartedFrame)"
                    )
                return  # dropped: never reaches the transport
            if self._armed:
                # Armed longer than the failsafe — pass through and rearm off.
                self._disarm("max-armed timeout exceeded")

        await self.push_frame(frame, direction)

    def _disarm(self, reason: str) -> None:
        logger.info(
            f"{self}: stale-audio guard disarmed ({reason}); "
            f"dropped {self._dropped} audio frame(s)"
        )
        self._armed = False
        self._dropped = 0
