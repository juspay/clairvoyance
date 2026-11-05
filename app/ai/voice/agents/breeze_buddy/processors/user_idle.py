"""User idle callback for Pipecat 1.1.0 LLMUserAggregator.

Pipecat 1.1.0 builds idle detection into ``LLMUserAggregator`` via
``LLMUserAggregatorParams.user_idle_timeout`` — the aggregator owns the
timer, restarts it on user activity, and pauses it during function
calls. This module provides only the application-level retry tracker:
prompt the user a few times, then end the call.

Wiring (see ``agent/pipeline.py``):

    handler = UserIdleCallbackHandler(...)

    @user_aggregator.event_handler("on_user_turn_idle")
    async def _on_idle(aggregator):
        await handler.handle_user_idle(aggregator)

Retries reset on user activity (see ``agent/__init__.py``):

    @user_aggregator.event_handler("on_user_turn_started")
    async def _on_started(aggregator, strategy):
        handler.reset_retry_count()
"""

import asyncio
from typing import Any, Awaitable, Callable, Optional

from pipecat.frames.frames import LLMMessagesAppendFrame

from app.core.logger import logger


class UserIdleCallbackHandler:
    """Tracks idle retries and ends the call when they exceed ``max_retries``."""

    def __init__(
        self,
        idle_message: str,
        max_retries: int,
        on_user_idle_timeout: Optional[Callable[[int], Awaitable[None]]] = None,
    ):
        self.idle_message = idle_message
        self.max_retries = max_retries
        self.on_user_idle_timeout = on_user_idle_timeout
        self.retry_count = 0
        # Set when end_conversation is in flight, cleared if it fails so
        # subsequent idle events can retry.
        self._call_ended = False
        # Optional predicate that pauses idle handling entirely while True
        # (e.g. an approval card is pending and the user is silently
        # reading it). Assigned post-construction by the agent.
        self.suppress_when: Optional[Callable[[], bool]] = None
        logger.info(f"User idle detection enabled with max_retries: {max_retries}")

    def reset_retry_count(self) -> None:
        """Reset the retry counter when the user re-engages."""
        if self.retry_count > 0:
            logger.debug(f"Resetting user idle retry counter (was {self.retry_count})")
            self.retry_count = 0

    async def handle_user_idle(self, aggregator: Any) -> None:
        """Prompt the user; end the call after ``max_retries`` prompts."""
        if self._call_ended:
            return

        if self.suppress_when is not None and self.suppress_when():
            logger.debug("User idle event suppressed (pending approval or similar)")
            return

        self.retry_count += 1
        logger.info(
            f"User idle detected (retry {self.retry_count - 1}, "
            f"max prompts: {self.max_retries})"
        )

        if self.retry_count > self.max_retries:
            self._call_ended = True
            logger.info(
                f"Max idle retries ({self.max_retries}) reached, ending call (BUSY)"
            )
            if not await self._trigger_end_call():
                self._call_ended = False
                logger.warning("Idle end-call failed; allowing further idle events")
            return

        logger.info("Prompting user to re-engage")
        await aggregator.push_frame(
            LLMMessagesAppendFrame(
                [{"role": "system", "content": self.idle_message}],
                run_llm=True,
            )
        )

    async def _trigger_end_call(self) -> bool:
        """Run the injected end-conversation callback with BUSY outcome."""
        if not self.on_user_idle_timeout:
            logger.error(
                "No on_user_idle_timeout callback configured "
                f"(retry_count={self.retry_count}, max_retries={self.max_retries})"
            )
            return False

        try:
            logger.info(
                "Triggering end_conversation "
                f"(BUSY, idle_retry_count={self.retry_count})"
            )
            await self.on_user_idle_timeout(self.retry_count)
            return True
        except asyncio.CancelledError:
            # Propagate during shutdown — the task manager is cancelling us.
            raise
        except Exception:
            logger.exception(
                "Error triggering on_user_idle_timeout "
                f"(retry_count={self.retry_count}, max_retries={self.max_retries})"
            )
            return False
