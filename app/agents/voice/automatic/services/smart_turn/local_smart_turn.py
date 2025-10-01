# app/agents/voice/automatic/services/local_smart_turn.py

from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3


class LocalSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """
    A wrapper around LocalSmartTurnAnalyzerV3 to add explicit resource management
    without modifying the original library code.
    """

    async def shutdown(self):
        """Release resources used by the ONNX session."""
        if hasattr(self, "_session") and self._session:
            # ONNX Runtime sessions don't have an explicit close method.
            # Setting the session to None allows the garbage collector to release it.
            self._session = None
            logger.debug("LocalSmartTurnAnalyzer shutdown and resources released.")
        else:
            logger.debug(
                "LocalSmartTurnAnalyzer shutdown called but no session to clean up."
            )
