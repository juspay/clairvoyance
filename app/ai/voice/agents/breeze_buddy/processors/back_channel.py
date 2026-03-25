"""
Back-Channel Processor

A lightweight FrameProcessor that sends soft audio acknowledgments ("okay",
"got it") during input collection accumulation windows. This reassures the
user that the agent is still listening while they dictate multi-segment input
(phone numbers, addresses, etc.) with natural pauses.

The processor is always in the pipeline but is a transparent passthrough when
disabled. It is enabled/disabled per-node by the transition handler based on
the node's input_collection.back_channel configuration.

Pipeline position:
    stt
    -> TranscriptionGateProcessor
    -> BackChannelProcessor         <- here
    -> [UserIdleProcessor]
    -> user_aggregator
    -> llm
    -> tts
    ...

When enabled:
1. Finalized TranscriptionFrame arrives -> schedule a back-channel timer
2. If InterimTranscriptionFrame arrives -> cancel timer (user resumed speaking)
3. If LLMFullResponseStartFrame arrives -> cancel timer (turn ended, LLM processing)
4. When timer fires -> check min_interval throttle -> push TTSSpeakFrame downstream
5. TTSSpeakFrame flows through user_aggregator/llm (both pass it through) to TTS
6. append_to_context=False prevents LLM context pollution
"""

import asyncio
import random
import time
from typing import Optional

from pipecat.frames.frames import (
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseStartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.ai.voice.agents.breeze_buddy.template.types import BackChannelConfig
from app.core.logger import logger


class BackChannelProcessor(FrameProcessor):
    """Sends soft back-channel acknowledgments during input collection.

    Transparent passthrough when disabled. Enable/disable via the public API
    during node transitions.
    """

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._enabled: bool = False
        self._delay_secs: float = 1.5
        self._min_interval_secs: float = 3.0
        self._messages: list[str] = []
        self._pending_timer: Optional[asyncio.Task] = None
        self._last_back_channel_time: float = 0.0

    # ------------------------------------------------------------------
    # Public API (called from transition handler)
    # ------------------------------------------------------------------

    def enable(self, config: BackChannelConfig) -> None:
        """Activate back-channeling with the given configuration."""
        self._cancel_timer()
        self._delay_secs = config.delay_secs
        self._min_interval_secs = config.min_interval_secs
        self._messages = list(config.messages) if config.messages else []
        if not self._messages:
            logger.warning(
                "BackChannel: enabled but messages list is empty, staying disabled"
            )
            self._enabled = False
            return
        self._enabled = True
        logger.info(
            f"BackChannel: enabled (delay={self._delay_secs}s, "
            f"min_interval={self._min_interval_secs}s, "
            f"messages={self._messages})"
        )

    def disable(self) -> None:
        """Deactivate back-channeling and cancel any pending timer."""
        if self._enabled:
            logger.info("BackChannel: disabled")
        self._cancel_timer()
        self._enabled = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        """Cancel the pending back-channel timer if any."""
        if self._pending_timer and not self._pending_timer.done():
            self._pending_timer.cancel()
            logger.debug("BackChannel: timer cancelled")
        self._pending_timer = None

    def _schedule_back_channel(self) -> None:
        """Cancel any existing timer and schedule a new one."""
        self._cancel_timer()
        self._pending_timer = asyncio.create_task(self._fire_back_channel())

    async def _fire_back_channel(self) -> None:
        """Wait delay_secs then push a TTSSpeakFrame if throttle allows."""
        try:
            await asyncio.sleep(self._delay_secs)

            # Check min_interval throttle
            now = time.monotonic()
            elapsed = now - self._last_back_channel_time
            if elapsed < self._min_interval_secs:
                logger.debug(
                    f"BackChannel: throttled (elapsed={elapsed:.1f}s < "
                    f"min_interval={self._min_interval_secs}s)"
                )
                return

            if not self._enabled or not self._messages:
                return

            message = random.choice(self._messages)
            self._last_back_channel_time = now
            logger.info(f"BackChannel: sending '{message}'")

            await self.push_frame(
                TTSSpeakFrame(text=message, append_to_context=False),
                FrameDirection.DOWNSTREAM,
            )
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # FrameProcessor implementation
    # ------------------------------------------------------------------

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if self._enabled:
            if isinstance(frame, TranscriptionFrame):
                # Finalized transcript -> schedule back-channel timer
                self._schedule_back_channel()

            elif isinstance(frame, InterimTranscriptionFrame):
                # User resumed speaking -> cancel pending back-channel
                self._cancel_timer()

        # Turn ended, LLM is processing -> cancel any pending back-channel
        if isinstance(frame, LLMFullResponseStartFrame):
            self._cancel_timer()

        # Always pass all frames through unchanged
        await self.push_frame(frame, direction)

    async def cleanup(self):
        """Cancel pending timer on pipeline shutdown."""
        self._cancel_timer()
        await super().cleanup()
