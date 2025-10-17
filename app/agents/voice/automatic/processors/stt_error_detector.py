"""
STT Error Detector Processor

Monitors ErrorFrames in the pipeline and triggers STT provider fallback
when STT-related errors are detected. Provides immediate response to
STT service failures for robust voice agent operation.
"""

from typing import Callable

from pipecat.frames.frames import ErrorFrame, Frame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from app.core.logger import logger


class STTErrorDetector(FrameProcessor):
    """
    Monitors pipeline frames for STT service errors and triggers fallback.

    When an STT-related ErrorFrame is detected, immediately triggers the
    restart callback to switch to the next available STT provider.
    """

    def __init__(
        self, restart_callback: Callable[[], None], name: str = "STTErrorDetector"
    ):
        """
        Initialize the STT error detector.

        Args:
            restart_callback: Async function to call when STT error detected
            name: Name for this processor instance
        """
        super().__init__(name=name)
        self.restart_callback = restart_callback
        logger.debug(f"STT Error Detector initialized: {name}")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """
        Process each frame, checking for STT-related errors.

        Args:
            frame: The frame to process
            direction: Direction of frame flow (upstream/downstream)
        """
        if isinstance(frame, ErrorFrame) and self._is_stt_error(frame):
            logger.error(f"STT error detected: {frame.error}")
            logger.info("Triggering STT provider fallback")

            # Trigger immediate restart with next provider
            await self.restart_callback()
            return  # Don't propagate STT errors further down pipeline

        # Pass all other frames through unchanged
        await self.push_frame(frame, direction)

    def _is_stt_error(self, error_frame: ErrorFrame) -> bool:
        """
        Determine if an ErrorFrame originated from an STT service.

        Checks error message for STT-specific keywords and patterns.

        Args:
            error_frame: The ErrorFrame to analyze

        Returns:
            True if error appears to be from STT service, False otherwise
        """
        error_msg = str(error_frame.error).lower()

        # STT service name patterns
        stt_services = [
            "soniox",
            "sonioxsttservice",
            "deepgram",
            "deepgramsttservice",
            "openai",
            "openaisttservice",
            "assemblyai",
            "assemblyaisttservice",
            "google",
            "googlesttservice",
        ]

        # Common STT error patterns
        stt_error_patterns = [
            "stt",
            "speech-to-text",
            "transcription",
            "timeout",
            "handshake",
            "websocket",
            "connection",
            "auth",
            "api key",
        ]

        # Check if error message contains any STT-related keywords
        for keyword in stt_services + stt_error_patterns:
            if keyword in error_msg:
                logger.debug(
                    f"STT error detected by keyword: '{keyword}' in '{error_msg}'"
                )
                return True

        return False
