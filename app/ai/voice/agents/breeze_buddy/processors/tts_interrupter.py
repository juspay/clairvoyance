"""
Audio Interruption Processor

This processor handles audio interruption for preventing double-speaking:
- When a NEW LLM response starts while a previous response is pending/playing,
  interrupt the old response so only the LATEST plays
- This ensures the user's most recent intent is honored

PLACEMENT: AFTER TTS (downstream in the pipeline)

Works with ResponseGateTracker (in response_gate.py) to coordinate state.
"""

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.processors.response_gate import ResponseGateState
from app.core.logger import logger


class AudioInterruptionProcessor(FrameProcessor):
    """
    Interrupts previous response when new LLM response starts.

    PLACEMENT: AFTER TTS (downstream in the pipeline)

    Strategy ("latest wins"):
    - When a NEW LLMFullResponseStartFrame arrives while a previous response
      is still pending (generating or playing), push BotStoppedSpeakingFrame
      UPSTREAM to interrupt the old response
    - This ensures the LATEST LLM response is what gets played

    Args:
        state: ResponseGateState instance shared with ResponseGateTracker.
               Each Agent should create its own state instance.
        name: Optional processor name for logging.
    """

    def __init__(
        self,
        state: ResponseGateState,
        name: str = "AudioInterruptionProcessor",
    ):
        super().__init__(name=name)
        self._state = state

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and interrupt previous response when new one starts."""
        await super().process_frame(frame, direction)

        # Track when TTS actually starts playing audio
        if isinstance(frame, BotStartedSpeakingFrame):
            self._state.tts_playing = True
            logger.debug("AudioInterruption: Bot started speaking")
            await self.push_frame(frame, direction)

        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._state.tts_playing = False
            self._state.response_pending = False
            logger.debug("AudioInterruption: Bot stopped speaking, response complete")
            await self.push_frame(frame, direction)

        # When new LLM response starts, interrupt if:
        # 1. TTS is currently playing audio (interrupt active playback)
        # 2. OR a previous LLM response is still pending (cancel queued audio)
        elif isinstance(frame, LLMFullResponseStartFrame):
            if self._state.tts_playing or self._state.response_pending:
                logger.debug(
                    "AudioInterruption: New LLM response started while previous active - "
                    f"INTERRUPTING (tts_playing={self._state.tts_playing}, "
                    f"response_pending={self._state.response_pending})"
                )
                # Push BotStoppedSpeakingFrame UPSTREAM to interrupt/cancel
                await self.push_frame(
                    BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM
                )
                self._state.tts_playing = False
                self._state.response_pending = False
            else:
                logger.debug(
                    "AudioInterruption: New LLM response starting (no prior pending)"
                )

            # Mark new response as pending
            self._state.response_pending = True
            await self.push_frame(frame, direction)

        elif isinstance(frame, LLMFullResponseEndFrame):
            logger.debug("AudioInterruption: LLM response ended")
            await self.push_frame(frame, direction)

        # Pass through all other frames
        else:
            await self.push_frame(frame, direction)
