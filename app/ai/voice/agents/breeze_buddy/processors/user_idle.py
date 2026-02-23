"""User idle processor for detecting and handling user inactivity."""

import asyncio
from typing import Awaitable, Callable, Optional

from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.user_idle_processor import (
    UserIdleProcessor as PipecatUserIdleProcessor,
)

from app.core.logger import logger


class UserIdleCallbackHandler:
    """Handles user idle detection with retry tracking and call termination.

    This class maintains state for tracking idle retries and handles
    the logic for prompting the user or ending the call after max retries.
    """

    def __init__(
        self,
        idle_message: str,
        max_retries: int,
        on_user_idle_timeout: Optional[Callable[[int], Awaitable[None]]] = None,
    ):
        """Initialize the callback handler.

        Args:
            idle_message: The system message to send when user is idle
            max_retries: Maximum number of idle prompts before ending call
            on_user_idle_timeout: Async callback that triggers full end_conversation flow
                                  with BUSY outcome. Takes idle_retry_count as argument.
        """
        self.idle_message = idle_message
        self.max_retries = max_retries
        self.on_user_idle_timeout = on_user_idle_timeout
        self.retry_count = 0
        self._call_ended = False  # Prevent duplicate call termination

    def reset_retry_count(self) -> None:
        """Reset the idle retry counter when user activity is detected.

        This should be called when the user speaks or otherwise re-engages,
        so that subsequent idle periods start counting retries from zero again.
        """
        if self.retry_count > 0:
            logger.debug(f"Resetting user idle retry counter (was {self.retry_count})")
            self.retry_count = 0

    async def handle_user_idle(self, processor: PipecatUserIdleProcessor) -> None:
        """Handle user idle event by prompting user or ending call.

        This callback is invoked each time the user is idle for the configured timeout.
        It tracks retry attempts and ends the call after max_retries is exceeded.
        """
        # Prevent duplicate execution after call has been ended
        if self._call_ended:
            logger.debug("Call already ended, ignoring idle event")
            return

        self.retry_count += 1
        logger.info(
            f"User idle detected (retry {self.retry_count - 1}, max prompts: {self.max_retries})"
        )

        if self.retry_count > self.max_retries:
            # Max retries exceeded - end the call
            # Set flag immediately to prevent race condition with concurrent idle events
            self._call_ended = True
            logger.info(
                f"Max idle retries ({self.max_retries}) reached, ending call with outcome: BUSY"
            )
            success = await self._trigger_end_call()
            if not success:
                # Reset flag so idle detection can retry or caller can handle
                self._call_ended = False
                logger.warning(
                    "Failed to end call via idle timeout, resetting _call_ended flag"
                )
        else:
            # Still have retries left - prompt the user
            logger.info("Prompting user to re-engage")
            await processor.push_frame(
                LLMMessagesAppendFrame(
                    [
                        {
                            "role": "system",
                            "content": self.idle_message,
                        }
                    ],
                    run_llm=True,
                )
            )

    async def _trigger_end_call(self) -> bool:
        """Trigger the full end_conversation flow with BUSY outcome.

        Uses the on_user_idle_timeout callback which triggers the Agent's
        end_conversation handler, ensuring transcription, errors,
        and all metadata are properly collected.

        Returns:
            True if the end call was triggered successfully, False otherwise.
        """
        if not self.on_user_idle_timeout:
            logger.error(
                "No on_user_idle_timeout callback configured - cannot end call. "
                f"retry_count={self.retry_count}, max_retries={self.max_retries}"
            )
            return False

        try:
            logger.info(
                "Triggering end_conversation flow with BUSY outcome "
                f"(idle_retry_count: {self.retry_count})"
            )
            await self.on_user_idle_timeout(self.retry_count)
            return True
        except asyncio.CancelledError:
            # Re-raise cancellation - this is expected during shutdown
            logger.debug("on_user_idle_timeout cancelled during shutdown")
            raise
        except Exception as e:
            logger.error(
                f"Error triggering on_user_idle_timeout: {e}. "
                f"retry_count={self.retry_count}, max_retries={self.max_retries}",
                exc_info=True,
            )
            return False


def create_user_idle_processor(
    enabled: bool,
    timeout: float,
    message: str,
    max_retries: int = 3,
    on_user_idle_timeout: Optional[Callable[[int], Awaitable[None]]] = None,
) -> tuple[PipecatUserIdleProcessor, UserIdleCallbackHandler] | None:
    """Create a user idle processor if enabled.

    Factory function that wraps Pipecat's UserIdleProcessor and provides
    a convenient interface for creating processors based on template configuration.

    Args:
        enabled: Whether user idle handling is enabled.
        timeout: Idle timeout in seconds.
        message: System message to prompt LLM when user is idle.
        max_retries: Maximum number of idle prompts before ending call. The call ends on the (max_retries+1)th idle event.
        on_user_idle_timeout: Async callback that triggers the full end_conversation
                              flow with BUSY outcome. Takes idle_retry_count as argument.
                              This ensures transcription, errors, and all metadata are
                              properly collected.

    Returns:
        Tuple of (PipecatUserIdleProcessor, UserIdleCallbackHandler) if enabled, None otherwise.
        The callback handler is returned so the caller can call reset_retry_count()
        when user activity is detected.

    Example:
        result = create_user_idle_processor(
            enabled=True,
            timeout=5.0,
            message="Custom idle message",
            max_retries=3,
            on_user_idle_timeout=agent._handle_user_idle_timeout,
        )
        if result:
            processor, callback_handler = result
            pipeline_parts.append(processor)
            # Call callback_handler.reset_retry_count() when user speaks
    """
    if not enabled:
        return None

    callback_handler = UserIdleCallbackHandler(
        idle_message=message,
        max_retries=max_retries,
        on_user_idle_timeout=on_user_idle_timeout,
    )

    user_idle = PipecatUserIdleProcessor(
        callback=callback_handler.handle_user_idle,
        timeout=timeout,
    )
    logger.info(
        f"User idle detection enabled with timeout: {timeout}s, max_retries: {max_retries}"
    )
    return user_idle, callback_handler
