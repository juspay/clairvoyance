"""
Response State Gate Processor - V2

Handles ALL interruption scenarios to prevent double-speaking:

1. STT arrives → LLM processing → New STT arrives
   → Cancel LLM, buffer new STT, process new STT

2. LLM + TTS both processing → New STT arrives
   → Cancel both, buffer new STT, process new STT

3. TTS is speaking → New STT arrives
   → Stop TTS, buffer new STT, process new STT

4. LLM processing (no TTS yet) → New STT arrives
   → Cancel LLM, buffer new STT, process new STT
"""

from enum import Enum, auto

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class ResponseState(Enum):
    """States for the response state machine."""

    IDLE = auto()  # No pending response
    LLM_PROCESSING = auto()  # LLM has context, generating response
    TTS_SPEAKING = auto()  # TTS is generating/playing audio
    BOTH = auto()  # Both LLM and TTS are active


class ResponseStateGate(FrameProcessor):
    """
    State machine that tracks LLM/TTS state and handles ALL interruptions.

    Key insight: When interrupting, we must BUFFER the new transcription
    until the interruption completes, then process ONLY the buffered frame.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._state = ResponseState.IDLE
        self._buffered_transcription = None  # Hold transcription during interruption
        self._interruption_in_progress = False

    @property
    def state(self) -> ResponseState:
        """Get current state."""
        return self._state

    async def _handle_transcription_frame(
        self, frame: TranscriptionFrame, direction: FrameDirection
    ):
        """Process a transcription frame, either buffered or new.

        This method handles transcription frames consistently whether they're
        fresh frames or buffered frames being flushed after an interruption.
        """
        logger.debug("ResponseGate: Processing transcription frame")
        self._state = ResponseState.LLM_PROCESSING
        await self.push_frame(frame, direction)

    async def _flush_buffered_transcription(self, direction: FrameDirection):
        """Process any buffered transcription immediately."""
        if self._buffered_transcription:
            buffered = self._buffered_transcription
            self._buffered_transcription = None
            await self._handle_transcription_frame(buffered, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # If interruption is in progress, buffer transcription frames.
        # Later frames will overwrite earlier ones, so only the latest
        # transcription is kept for processing after interruption completes.
        if self._interruption_in_progress and isinstance(frame, TranscriptionFrame):
            self._buffered_transcription = frame
            logger.debug(
                f"ResponseGate: Buffering new transcription during interruption "
                f"(id={id(frame)})"
            )
            return  # Don't push, wait for interruption to finish

        # Track LLM state (only when NOT buffering)
        if isinstance(frame, LLMFullResponseStartFrame):
            if self._state == ResponseState.IDLE:
                self._state = ResponseState.LLM_PROCESSING
                logger.debug("ResponseGate: LLM_PROCESSING")
            elif self._state == ResponseState.TTS_SPEAKING:
                self._state = ResponseState.BOTH
                logger.debug("ResponseGate: BOTH")

        elif isinstance(frame, LLMFullResponseEndFrame):
            if self._state == ResponseState.BOTH:
                self._state = ResponseState.TTS_SPEAKING
                logger.debug("ResponseGate: TTS_SPEAKING (LLM finished)")

        # Track TTS/Output state
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._state == ResponseState.IDLE:
                self._state = ResponseState.TTS_SPEAKING
                logger.debug("ResponseGate: TTS_SPEAKING (initial)")
            elif self._state == ResponseState.LLM_PROCESSING:
                self._state = ResponseState.BOTH
                logger.debug("ResponseGate: BOTH")

        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._state == ResponseState.TTS_SPEAKING:
                self._state = ResponseState.IDLE
                logger.debug("ResponseGate: IDLE")
            elif self._state == ResponseState.BOTH:
                self._state = ResponseState.LLM_PROCESSING
                logger.debug("ResponseGate: LLM_PROCESSING (TTS stopped)")

        # Handle new transcription - THE KEY INTERRUPTION LOGIC
        elif isinstance(frame, TranscriptionFrame):
            if self._state != ResponseState.IDLE:
                # We have an active response, need to interrupt
                logger.debug(
                    f"ResponseGate: Interrupting state={self._state.name} "
                    f"for new transcription (id={id(frame)})"
                )
                # Push interruption to cancel current LLM/TTS
                self._interruption_in_progress = True
                # Buffer this frame BEFORE starting interruption
                # Any later frames will overwrite this buffer during the await
                self._buffered_transcription = frame
                await self.push_interruption_task_frame_and_wait()

                # Interruption complete - DON'T overwrite buffer!
                # The buffer already contains the latest transcription (if any
                # arrived during the await, it would have overwritten the buffer)
                self._interruption_in_progress = False
                self._state = ResponseState.IDLE

                # Flush whatever is currently buffered (may be the original
                # frame or a newer one that arrived during the await)
                await self._flush_buffered_transcription(direction)
                return

            # No active response, just process normally
            await self._handle_transcription_frame(frame, direction)
            return

        # Pass all other frames through
        await self.push_frame(frame, direction)
