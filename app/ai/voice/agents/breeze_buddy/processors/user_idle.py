"""User idle processor for detecting and handling user inactivity."""

from pipecat.frames.frames import LLMMessagesAppendFrame
from pipecat.processors.user_idle_processor import (
    UserIdleProcessor as PipecatUserIdleProcessor,
)

from app.core.logger import logger


def _create_idle_callback(idle_message: str):
    """Create a callback function for user idle detection.

    Args:
        idle_message: The system message to send when user is idle

    Returns:
        Async callback function for UserIdleProcessor
    """

    async def handle_user_idle(processor: PipecatUserIdleProcessor) -> None:
        """Handle user idle by prompting the user."""
        logger.info("User idle detected, prompting user")
        await processor.push_frame(
            LLMMessagesAppendFrame(
                [
                    {
                        "role": "system",
                        "content": idle_message,
                    }
                ],
                run_llm=True,
            )
        )

    return handle_user_idle


def create_user_idle_processor(
    enabled: bool,
    timeout: float,
    message: str,
) -> PipecatUserIdleProcessor | None:
    """Create a user idle processor if enabled.

    Factory function that wraps Pipecat's UserIdleProcessor and provides
    a convenient interface for creating processors based on template configuration.

    Args:
        enabled: Whether user idle handling is enabled.
        timeout: Idle timeout in seconds.
        message: System message to prompt LLM when user is idle.

    Returns:
        PipecatUserIdleProcessor instance if enabled, None otherwise

    Example:
        processor = create_user_idle_processor(
            enabled=True,
            timeout=5.0,
            message="Custom idle message",
        )
        if processor:
            pipeline_parts.append(processor)
    """
    if not enabled:
        return None

    user_idle = PipecatUserIdleProcessor(
        callback=_create_idle_callback(message),
        timeout=timeout,
    )
    logger.info(f"User idle detection enabled with timeout: {timeout}s")
    return user_idle
