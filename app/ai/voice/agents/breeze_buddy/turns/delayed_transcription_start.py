"""Delayed transcription-based user turn start strategy.

Provides a fallback turn start mechanism that gives VAD priority. When VAD
misses speech (soft voice, volume smoothing lag, brief utterances), this
strategy catches it via STT transcription after a configurable delay.

The delay ensures:
1. VAD has time to fire first (~96-224ms for typical speech)
2. Background noise producing transient transcriptions doesn't trigger false starts
3. Only sustained, finalized transcriptions trigger the fallback
"""

import asyncio
from typing import Optional

from loguru import logger

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    TranscriptionFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.turns.user_start.base_user_turn_start_strategy import (
    BaseUserTurnStartStrategy,
)
from pipecat.utils.asyncio.task_manager import BaseTaskManager


class DelayedTranscriptionUserTurnStartStrategy(BaseUserTurnStartStrategy):
    """Fallback turn start strategy that triggers after a configurable delay.

    Gives VAD priority to detect speech start. If VAD doesn't fire within
    the delay window but STT produces a transcription, this strategy triggers
    the turn start as a fallback.

    This handles cases where:
    - User speaks too softly for VAD (volume below min_volume threshold)
    - Volume smoothing lag prevents VAD from reaching threshold in time
    - Brief utterances end before VAD accumulates enough start_secs frames

    Background noise protection:
    - With use_interim=False (default), only finalized transcriptions trigger
      the fallback. Noise artifacts that Soniox doesn't finalize are ignored.
    - The delay window gives VAD time to fire. If VAD fires, the delayed
      trigger is cancelled (VAD handled it).
    - The UserTurnController deduplicates: if VAD already started the turn,
      this strategy's trigger is a no-op.

    Args:
        delay: Seconds to wait before triggering fallback. Default 0.5s.
            Should be longer than worst-case VAD start detection time (~224ms
            for strong voice with volume smoothing at 8kHz).
        use_interim: If True, interim transcriptions also trigger the delay.
            Default False for noise protection — only finalized transcriptions
            from Soniox trigger the fallback.
        enable_interruptions: Passed to base class. Default False since this
            is a fallback path.
    """

    def __init__(
        self,
        *,
        delay: float = 0.5,
        use_interim: bool = False,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self._delay = delay
        self._use_interim = use_interim
        self._vad_speaking = False
        self._delay_task: Optional[asyncio.Task] = None

    async def setup(self, task_manager: BaseTaskManager):
        await super().setup(task_manager)

    async def cleanup(self):
        await super().cleanup()
        if self._delay_task:
            await self.task_manager.cancel_task(self._delay_task)
            self._delay_task = None

    async def reset(self):
        await super().reset()
        if self._delay_task:
            await self.task_manager.cancel_task(self._delay_task)
            self._delay_task = None
        self._vad_speaking = False

    async def process_frame(self, frame: Frame):
        await super().process_frame(frame)

        # Track VAD state — if VAD fires, it handled speech detection.
        # Cancel any pending fallback trigger.
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self._vad_speaking = True
            if self._delay_task:
                await self.task_manager.cancel_task(self._delay_task)
                self._delay_task = None
            return

        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self._vad_speaking = False
            return

        # Don't trigger fallback while VAD is actively tracking speech
        if self._vad_speaking:
            return

        # Check if this transcription should start the delay timer
        is_relevant = False
        if isinstance(frame, InterimTranscriptionFrame) and self._use_interim:
            is_relevant = True
        elif isinstance(frame, TranscriptionFrame):
            is_relevant = True

        # Start delay timer on first relevant transcription.
        # Don't restart on subsequent transcriptions — the first one
        # already started the clock.
        if is_relevant and not self._delay_task:
            logger.debug(
                "DelayedTranscriptionStart: transcription received without VAD, "
                f"starting {self._delay}s fallback timer"
            )
            self._delay_task = self.task_manager.create_task(
                self._delayed_trigger(), f"{self}::_delayed_trigger"
            )

    async def _delayed_trigger(self):
        """Wait for delay then trigger turn start if VAD still hasn't fired."""
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        finally:
            self._delay_task = None

        # Only trigger if VAD still hasn't detected speech
        if not self._vad_speaking:
            logger.info(
                "DelayedTranscriptionStart: VAD missed speech, "
                "triggering fallback turn start"
            )
            await self.trigger_user_turn_started()
