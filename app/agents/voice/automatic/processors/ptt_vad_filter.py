"""
PTT VAD Filter Processor

Implements a belt-and-suspenders approach to drop VAD frames while PTT is active
and for a small cooldown period after PTT release.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from pipecat.frames.frames import (
    Frame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from app.core.logger import logger

# PTT cooldown configuration (in seconds)
PTT_COOLDOWN_SECS = 0.5
# PTT timeout configuration (maximum PTT duration before auto-reset)
PTT_MAX_DURATION_SECS = 60


class PTTVADFilter(FrameProcessor):
    """
    PTT-aware VAD filter that drops VAD frames during PTT active periods
    and cooldown after PTT release. Includes timeout safety to prevent
    stuck PTT states.
    """

    def __init__(self, name: str = "PTTVADFilter"):
        super().__init__(name=name)
        self._ptt_active = False
        self._ptt_cooldown_until: Optional[datetime] = None
        self._ptt_start_time: Optional[datetime] = None

    def set_ptt_active(self, active: bool) -> None:
        """Set PTT active state"""
        self._ptt_active = active

        if active:
            self._ptt_start_time = datetime.now(timezone.utc)
            self._ptt_cooldown_until = None
            logger.debug("PTT activated")
        else:
            self._ptt_start_time = None
            self._ptt_cooldown_until = datetime.now(timezone.utc) + timedelta(
                seconds=PTT_COOLDOWN_SECS
            )
            logger.debug("PTT released")

    def _should_pass_frame(self, frame: Frame) -> bool:
        """Determine if frame should pass through the filter"""

        # Auto-recovery from stuck PTT state (timeout safety)
        if self._ptt_active and self._ptt_start_time:
            duration = datetime.now(timezone.utc) - self._ptt_start_time
            if duration.total_seconds() > PTT_MAX_DURATION_SECS:
                logger.warning(
                    f"PTT timeout after {duration.total_seconds():.1f}s - auto-clearing stuck state"
                )
                self._ptt_active = False
                self._ptt_start_time = None
                self._ptt_cooldown_until = None

        # Allow emulated frames with explicit flag
        if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame)):
            if getattr(frame, "emulated", False):
                return True

        # Drop VAD-originated frames if PTT is active or during cooldown
        if isinstance(
            frame,
            (
                VADUserStartedSpeakingFrame,
                VADUserStoppedSpeakingFrame,
                UserStartedSpeakingFrame,
                UserStoppedSpeakingFrame,
            ),
        ):
            if self._ptt_active:
                logger.debug(f"Dropping {type(frame).__name__} - PTT active")
                return False
            if (
                self._ptt_cooldown_until
                and datetime.now(timezone.utc) < self._ptt_cooldown_until
            ):
                logger.debug(f"Dropping {type(frame).__name__} - PTT cooldown active")
                return False

        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        """Process frames and filter VAD frames based on PTT state"""
        await super().process_frame(frame, direction)

        if self._should_pass_frame(frame):
            await self.push_frame(frame, direction)
        else:
            # Frame is dropped, don't pass it along
            return
