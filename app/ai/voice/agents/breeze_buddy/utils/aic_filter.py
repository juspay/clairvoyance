"""Fail-open wrapper around pipecat's ai-coustics (AIC) input filter.

pipecat's AICFilter can raise out of two places that end a call:

- ``start()``: a corrupt or unreadable model file raises from the model load.
  The input transport awaits ``start()`` unguarded, so the transport never
  starts and the call fails.
- ``filter()``: the SDK raises mid-call (e.g. ``EnhancementNotAllowedError``
  when the license is revoked or usage reporting fails). The input transport's
  audio loop only catches ``TimeoutError``, so the exception kills the loop and
  the bot stops hearing the caller for the rest of the call.

Noise cancellation is an enhancement, never a dependency: on any SDK error this
filter disables itself for the rest of the call and passes the caller's raw
audio through. A new call builds a new filter, so it retries AIC from scratch.
"""

from pipecat.audio.filters.aic_filter import AICFilter
from pipecat.frames.frames import FilterControlFrame

from app.core.logger import logger


class FailOpenAICFilter(AICFilter):
    """AICFilter that degrades to passthrough instead of breaking the call."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._failed = False

    def _disable(self, stage: str, error: Exception) -> None:
        self._failed = True
        self._aic_ready = False
        logger.error(
            "AIC filter failed during {}; disabled for the rest of the call, "
            "passing raw audio through: {}: {}",
            stage,
            type(error).__name__,
            error,
        )

    async def start(self, sample_rate: int):
        try:
            await super().start(sample_rate)
        except Exception as e:
            self._disable("start", e)
            # Release whatever start() acquired before it raised (shared model
            # reference); stop() is idempotent, so the transport's own stop()
            # later is harmless.
            await self.stop()

    async def stop(self):
        try:
            await super().stop()
        except Exception as e:
            # super().stop() always finishes its cleanup in a finally; only the
            # processor reset can raise, and the call is ending anyway.
            logger.warning("AIC filter stop failed: {}: {}", type(e).__name__, e)

    async def process_frame(self, frame: FilterControlFrame):
        if self._failed:
            return
        try:
            await super().process_frame(frame)
        except Exception as e:
            self._disable("process_frame", e)

    async def filter(self, audio: bytes) -> bytes:
        if self._failed:
            return audio
        # super().filter() moves buffered audio out of the buffer before
        # processing it, so keep the raw bytes to hand back if it raises.
        pending = bytes(self._audio_buffer) + audio
        try:
            return await super().filter(audio)
        except Exception as e:
            self._disable("filter", e)
            self._audio_buffer.clear()
            return pending
