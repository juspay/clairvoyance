"""Local Smart Turn Analyzer for Breeze Buddy agent."""

from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3


class LocalSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """Wrapper around LocalSmartTurnAnalyzerV3 for proper resource management.

    Adds explicit shutdown method for ONNX session cleanup without modifying
    the original pipecat library code.
    """

    async def shutdown(self):
        """Release resources used by the ONNX session."""
        if hasattr(self, "_session") and self._session:
            # ONNX Runtime sessions don't have an explicit close method.
            # Setting the session to None allows the garbage collector to release it.
            self._session = None  # type: ignore
            logger.debug("LocalSmartTurnAnalyzer shutdown and resources released.")
        else:
            logger.debug(
                "LocalSmartTurnAnalyzer shutdown called but no session to clean up."
            )
