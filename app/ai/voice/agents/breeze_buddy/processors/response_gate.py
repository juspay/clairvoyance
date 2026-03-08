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
    InterimTranscriptionFrame,
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
        self,
        frame: "TranscriptionFrame | InterimTranscriptionFrame",
        direction: FrameDirection,
    ):
        """Process a transcription frame, either buffered or new.

        This method handles transcription frames consistently whether they're
        fresh frames or buffered frames being flushed after an interruption.
        """
        logger.debug("ResponseGate: Processing transcription frame")
        self._state = ResponseState.LLM_PROCESSING
        await self.push_frame(frame, direction)

    async def _flush_buffered_transcription(self, direction: FrameDirection):
        """Process any buffered transcription immediately.

        Only flushes final TranscriptionFrames. If an InterimTranscriptionFrame
        is buffered (e.g. bot stopped speaking before the final arrived), it is
        discarded — the final TranscriptionFrame will arrive shortly and be
        processed normally at IDLE state. Flushing an interim would set
        state=LLM_PROCESSING, and the subsequent final would then be stored
        with no completion event to flush it, causing a stalled turn.
        """
        if self._buffered_transcription:
            buffered = self._buffered_transcription
            self._buffered_transcription = None
            if isinstance(buffered, InterimTranscriptionFrame):
                logger.debug(
                    "ResponseGate: Discarding buffered interim transcription, "
                    "waiting for final"
                )
                return
            await self._handle_transcription_frame(buffered, direction)

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        # If interruption is in progress, buffer transcription frames.
        # Later frames will overwrite earlier ones, so only the latest
        # transcription is kept for processing after interruption completes.
        if self._interruption_in_progress and isinstance(
            frame, (TranscriptionFrame, InterimTranscriptionFrame)
        ):
            self._buffered_transcription = frame
            logger.debug(
                f"ResponseGate: Buffering new transcription during interruption "
                f"(id={id(frame)})"
            )
            return  # Don't push, wait for interruption to finish

        # Track TTS/Bot speaking state.
        # NOTE: LLMFullResponseStartFrame / LLMFullResponseEndFrame flow
        # downstream (toward TTS/Output) and never reach this processor,
        # so we rely solely on BotStarted/BotStopped frames which flow
        # upstream through the pipeline.
        if isinstance(frame, BotStartedSpeakingFrame):
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
                # BotStoppedSpeaking after BOTH means the last audio chunk
                # has finished playing. By this point the LLM has also
                # finished generating (all tokens consumed by TTS), so the
                # correct transition is BOTH → IDLE, not BOTH → LLM_PROCESSING.
                self._state = ResponseState.IDLE
                logger.debug("ResponseGate: IDLE (bot finished speaking)")

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
        # InterimTranscriptionFrame must also be caught here: the downstream
        # LLMUserAggregator uses TranscriptionUserTurnStartStrategy with
        # use_interim=True, so any interim frame that leaks past this gate
        # will trigger an InterruptionTaskFrame — killing TTS even when the
        # mode is DISABLED_WITH_STORE or DISABLED_WITHOUT_STORE.
        #
        # NOTE: _buffered_transcription is a single slot, not a list.
        # Each new frame (interim or final) overwrites the previous one.
        # This is safe because Soniox interim transcriptions are cumulative —
        # each interim contains all previously spoken text, and the final
        # TranscriptionFrame contains the complete utterance. Only the last
        # stored frame matters.
        elif isinstance(frame, (TranscriptionFrame, InterimTranscriptionFrame)):
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

            # No active response — only forward final TranscriptionFrames.
            # InterimTranscriptionFrames at IDLE carry no value (the final
            # frame will follow with the complete text) and must be dropped:
            # pushing an interim downstream would set state=LLM_PROCESSING
            # and cause subsequent frames (including the real final) to be
            # buffered with no completion event to flush them.
            if isinstance(frame, InterimTranscriptionFrame):
                return
            await self._handle_transcription_frame(frame, direction)
            return

        # Pass all other frames through
        await self.push_frame(frame, direction)
