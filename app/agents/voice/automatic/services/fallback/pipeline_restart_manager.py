"""
Pipeline restart manager with generic error detection for STT fallback scenarios.
"""

from pipecat.frames.frames import ErrorFrame

from app.core.logger import logger


class PipelineRestartManager:
    """Manages pipeline restart decisions based on STT provider errors."""

    def __init__(self):
        pass

    def is_soniox_error(self, error_frame: ErrorFrame) -> bool:
        """
        Check if the error is from Soniox STT service.

        Args:
            error_frame: The ErrorFrame object from the pipeline

        Returns:
            True if the error is from Soniox, False otherwise
        """
        if not isinstance(error_frame, ErrorFrame):
            return False

        # Check error message for Soniox-specific patterns
        error_message = str(error_frame.error).lower()

        # Look for Soniox-specific error patterns
        soniox_patterns = [
            "soniox",
            "timed out during handshake",
            "websocket connection failed",
            "connection timeout",
            "handshake timeout",
        ]

        for pattern in soniox_patterns:
            if pattern in error_message:
                logger.debug(
                    f"Detected Soniox error pattern: {pattern} in {error_message}"
                )
                return True

        return False

    def should_enable_fallback(
        self, error_frame: ErrorFrame, current_stt_provider: str, fallback_enabled: bool
    ) -> bool:
        """
        Determine if fallback should be enabled based on the error and current configuration.

        Args:
            error_frame: The ErrorFrame object from the pipeline
            current_stt_provider: The currently active STT provider name
            fallback_enabled: Whether fallback is enabled in configuration

        Returns:
            True if fallback should be triggered, False otherwise
        """
        if not fallback_enabled:
            logger.debug("Fallback disabled in configuration")
            return False

        if not isinstance(error_frame, ErrorFrame):
            logger.debug("Not an ErrorFrame, skipping fallback")
            return False

        # For now, only handle Soniox errors, but this can be extended
        if current_stt_provider.lower() == "soniox":
            if self.is_soniox_error(error_frame):
                logger.info(
                    f"Soniox error detected, enabling fallback: {error_frame.error}"
                )
                return True

        logger.debug(
            f"No fallback needed for {current_stt_provider} error: {error_frame.error}"
        )
        return False
