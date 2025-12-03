# app/agents/voice/automatic/services/local_smart_turn.py


from loguru import logger
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

try:
    from onnxruntime import InferenceSession
except ImportError:
    InferenceSession = None


class LocalSmartTurnAnalyzer(LocalSmartTurnAnalyzerV3):
    """
    A wrapper around LocalSmartTurnAnalyzerV3 to add explicit resource management
    without modifying the original library code.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Store the session reference without type annotation to avoid override issues
        # This allows us to handle None values safely
        current_session = getattr(self, "_session", None)
        if current_session is None:
            logger.debug("LocalSmartTurnAnalyzer initialized with no session")

    async def shutdown(self):
        """Release resources used by the ONNX session."""
        if hasattr(self, "_session") and self._session:
            # ONNX Runtime sessions don't have an explicit close method.
            # Setting the session to None allows the garbage collector to release it.
            # Use setattr to bypass type checking since we need to set it to None
            setattr(self, "_session", None)
            logger.debug("LocalSmartTurnAnalyzer shutdown and resources released.")
        else:
            logger.debug(
                "LocalSmartTurnAnalyzer shutdown called but no session to clean up."
            )
