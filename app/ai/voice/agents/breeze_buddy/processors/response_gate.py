"""
Response State Gate Processor - V3

Handles ALL interruption scenarios with CONCATENATING buffer to preserve
all user speech during rapid multi-part utterances.

Key improvements:
- Transcriptions arriving during interruption are CONCATENATED, not overwritten
- Prevents loss of intermediate speech like "of apples" in multi-part questions
- Buffer is cleared only when bot returns to IDLE state

Example:
  T=0ms:   "what is the price" → IDLE, passes through → LLM starts
  T=800ms: "of apples" → BUSY, buffer="of apples", interrupt sent
  T=1200ms: "actually oranges" → BUSY, buffer="of apples actually oranges" (concatenated)
  → After interruption completes, sends "of apples actually oranges" to LLM

Result: LLM context has ["what is the price", "of apples actually oranges"]
Previous behavior: LLM would get ["what is the price", "actually oranges"] (losing "of apples")
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

        # If interruption is in progress, buffer and CONCATENATE transcription frames.
        # This prevents losing intermediate speech during rapid multi-part utterances.
        if self._interruption_in_progress and isinstance(frame, TranscriptionFrame):
            if self._buffered_transcription:
                # Concatenate new text with existing buffer
                old_text = self._buffered_transcription.text
                new_text = frame.text
                combined_text = old_text + " " + new_text
                # Update frame with concatenated text, preserving latest metadata
                frame.text = combined_text
                logger.debug(
                    f"ResponseGate: Concatenating transcription during interruption "
                    f"(buffer: '{old_text}' + new: '{new_text}' = combined: '{combined_text}')"
                )
            else:
                logger.debug(
                    f"ResponseGate: Buffering first transcription during interruption "
                    f"(text: '{frame.text}')"
                )
            self._buffered_transcription = frame
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
                # Clear buffer when bot fully returns to IDLE after completing response
                self._buffered_transcription = None
                logger.debug("ResponseGate: IDLE (buffer cleared)")
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
