"""
Response State Gate Processor - V2

Handles interruption scenarios based on the configured InterruptionMode:

Mode: ENABLED (default)
  User speaks while bot is active → interrupt bot, buffer & process user speech.

Mode: DISABLED_WITH_STORE
  User speaks while bot is active → do NOT interrupt, buffer user speech,
  flush it downstream once the bot finishes speaking.

Mode: DISABLED_WITHOUT_STORE
  User speaks while bot is active → do NOT interrupt, silently discard
  user speech.
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

from app.ai.voice.agents.breeze_buddy.template.types import InterruptionMode
from app.core.logger import logger


class ResponseState(Enum):
    """States for the response state machine."""

    IDLE = auto()  # No pending response
    LLM_PROCESSING = auto()  # LLM has context, generating response
    TTS_SPEAKING = auto()  # TTS is generating/playing audio
    BOTH = auto()  # Both LLM and TTS are active


class ResponseStateGate(FrameProcessor):
    """
    State machine that tracks LLM/TTS state and handles interruptions
    according to the configured InterruptionMode.

    - ENABLED: interrupt bot, buffer new transcription, process after interruption.
    - DISABLED_WITH_STORE: don't interrupt, buffer transcription, flush when bot goes IDLE.
    - DISABLED_WITHOUT_STORE: don't interrupt, drop transcription silently.
    """

    def __init__(
        self,
        interruption_mode: InterruptionMode = InterruptionMode.ENABLED,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._state = ResponseState.IDLE
        self._buffered_transcription = None  # Hold transcription during interruption
        self._interruption_in_progress = False
        self._interruption_mode = interruption_mode
        logger.info(f"ResponseGate: initialized with mode={interruption_mode.value}")

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
            elif self._state == ResponseState.LLM_PROCESSING:
                # LLM finished without TTS (e.g. empty response) → back to IDLE
                self._state = ResponseState.IDLE
                logger.debug("ResponseGate: IDLE (LLM finished, no TTS)")

                if (
                    self._interruption_mode == InterruptionMode.DISABLED_WITH_STORE
                    and self._buffered_transcription
                ):
                    logger.info(
                        "ResponseGate: LLM finished without TTS, flushing stored transcription"
                    )
                    await self.push_frame(frame, direction)
                    await self._flush_buffered_transcription(FrameDirection.DOWNSTREAM)
                    return

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

            # DISABLED_WITH_STORE: flush buffered transcription when bot goes idle
            if (
                self._state == ResponseState.IDLE
                and self._interruption_mode == InterruptionMode.DISABLED_WITH_STORE
                and self._buffered_transcription
            ):
                logger.info(
                    "ResponseGate: Bot finished speaking, flushing stored transcription"
                )
                # Forward the BotStoppedSpeakingFrame first so downstream
                # processors see the bot-stopped event before the new
                # transcription arrives.
                await self.push_frame(frame, direction)
                await self._flush_buffered_transcription(FrameDirection.DOWNSTREAM)
                return

        # Handle new transcription - THE KEY INTERRUPTION LOGIC
        elif isinstance(frame, TranscriptionFrame):
            if self._state != ResponseState.IDLE:
                # Bot is active — behaviour depends on mode
                if self._interruption_mode == InterruptionMode.ENABLED:
                    # Interrupt bot: cancel LLM/TTS and process user speech
                    logger.debug(
                        f"ResponseGate: Interrupting state={self._state.name} "
                        f"for new transcription (id={id(frame)})"
                    )
                    self._interruption_in_progress = True
                    self._buffered_transcription = frame
                    await self.push_interruption_task_frame_and_wait()

                    self._interruption_in_progress = False
                    self._state = ResponseState.IDLE

                    await self._flush_buffered_transcription(direction)
                    return

                elif self._interruption_mode == InterruptionMode.DISABLED_WITH_STORE:
                    # Don't interrupt, but keep the latest transcription for later
                    self._buffered_transcription = frame
                    logger.debug(
                        f"ResponseGate: Storing transcription while bot active "
                        f"(mode=disabled_with_store, state={self._state.name})"
                    )
                    return

                else:
                    # DISABLED_WITHOUT_STORE — silently discard
                    logger.debug(
                        f"ResponseGate: Discarding transcription while bot active "
                        f"(mode=disabled_without_store, state={self._state.name})"
                    )
                    return

            # No active response, just process normally
            await self._handle_transcription_frame(frame, direction)
            return

        # Pass all other frames through
        await self.push_frame(frame, direction)
