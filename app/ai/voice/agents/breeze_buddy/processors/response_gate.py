"""
Response Gate State Tracker

This module tracks state for preventing double-speaking:
- ALL transcriptions are allowed through to LLM (no blocking)
- When a NEW LLM response starts while a previous one is pending/playing,
  the AudioInterruptionProcessor will interrupt the old response

PLACEMENT: BEFORE the LLM (upstream in the pipeline)

The processor uses ResponseGateState which should be shared with AudioInterruptionProcessor
(located in tts_interrupter.py) to coordinate interruptions.

Each Agent instance should create its own ResponseGateState to avoid cross-talk.
"""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class ResponseGateState:
    """
    Holds the shared state for response gate processors.

    Each Agent instance should create its own ResponseGateState to avoid
    cross-talk between concurrent agents.

    Usage:
        state = ResponseGateState()
        response_gate = ResponseGateTracker(state=state)
        audio_interrupter = AudioInterruptionProcessor(state=state)
    """

    def __init__(self):
        self.tts_playing: bool = False  # True when TTS audio is playing
        # True from LLMFullResponseStart until BotStoppedSpeaking
        # Covers the gap between LLM finishing and TTS starting
        self.response_pending: bool = False

    def reset(self):
        """Reset all state when bot stops responding."""
        self.tts_playing = False
        self.response_pending = False

    def __repr__(self) -> str:
        return (
            f"ResponseGateState(tts_playing={self.tts_playing}, "
            f"response_pending={self.response_pending})"
        )


class ResponseGateTracker(FrameProcessor):
    """
    Tracks state for response gate strategy - ALLOWS ALL transcriptions through.

    PLACEMENT: BEFORE the LLM (upstream in the pipeline)

    This processor does NOT block any transcriptions. It only tracks state
    so that AudioInterruptionProcessor knows when to interrupt old responses.

    The actual interruption happens in AudioInterruptionProcessor when it sees
    a new LLMFullResponseStartFrame while a previous response is still pending.

    Args:
        state: ResponseGateState instance shared with AudioInterruptionProcessor.
               Each Agent should create its own state instance.
        name: Optional processor name for logging.
    """

    def __init__(
        self,
        state: ResponseGateState,
        name: str = "ResponseGateTracker",
    ):
        super().__init__(name=name)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames - track state and interrupt when new transcription arrives."""
        await super().process_frame(frame, direction)

        # Track LLM generation state
        if isinstance(frame, LLMFullResponseStartFrame):
            self._state.response_pending = True
            logger.debug("ResponseGate: LLM response started (response_pending=True)")
            await self.push_frame(frame, direction)

        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.debug("ResponseGate: LLM response ended")
            await self.push_frame(frame, direction)

        # Track TTS state
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._state.tts_playing = True
            logger.debug("ResponseGate: TTS started playing")
            await self.push_frame(frame, direction)

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.tts_playing = False
            self._state.response_pending = False
            logger.debug("ResponseGate: TTS stopped, response complete")
            await self.push_frame(frame, direction)

        # Handle new transcriptions - interrupt if previous response is still active
        elif isinstance(frame, TranscriptionFrame):
            # If a previous response is still pending or TTS is playing,
            # interrupt it BEFORE allowing the new transcription through.
            # This is the "latest wins" strategy - new user input takes priority.
            if self._state.tts_playing or self._state.response_pending:
                logger.debug(
                    f"ResponseGate: New transcription while response active - INTERRUPTING "
                    f"(tts_playing={self._state.tts_playing}, "
                    f"response_pending={self._state.response_pending})"
                )
                # Push InterruptionFrame DOWNSTREAM - this triggers pipecat's
                # interruption mechanism which stops TTS playback and clears queues
                # Note: Don't use CancelFrame as it's too aggressive and cancels new requests too
                await self.push_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
                self._state.tts_playing = False
                self._state.response_pending = False

            logger.debug(
                f"ResponseGate: Allowing transcription: '{frame.text}' "
                f"(tts_playing={self._state.tts_playing}, "
                f"response_pending={self._state.response_pending})"
            )
            await self.push_frame(frame, direction)

        else:
            # Pass through all other frames
            await self.push_frame(frame, direction)
