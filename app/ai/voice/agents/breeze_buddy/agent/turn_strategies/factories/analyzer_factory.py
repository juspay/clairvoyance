"""Factory for creating turn analyzer instances."""

from typing import Any, Dict, Optional

from pipecat.audio.turn.base_turn_analyzer import BaseTurnAnalyzer

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    TurnAnalyzerType,
)
from app.ai.voice.agents.breeze_buddy.services.local_smart_turn import (
    LocalSmartTurnAnalyzer,
)
from app.core.logger import logger


class TurnAnalyzerFactory:
    """Factory for creating turn analyzer instances."""

    @staticmethod
    def create(
        analyzer_type: str, params: Optional[Dict[str, Any]] = None
    ) -> BaseTurnAnalyzer:
        """Create a turn analyzer from type and parameters.

        Args:
            analyzer_type: Type of analyzer to create (only "local_smart_turn_v3" supported)
            params: Optional parameters for the analyzer

        Returns:
            Instantiated turn analyzer

        Raises:
            ImportError: If analyzer dependencies are not installed
            ValueError: If analyzer type is not supported
        """
        params = params or {}

        # Only support local_smart_turn_v3
        if analyzer_type != TurnAnalyzerType.LOCAL_SMART_TURN_V3:
            raise ValueError(
                f"Only '{TurnAnalyzerType.LOCAL_SMART_TURN_V3}' analyzer is supported. "
                f"Got: {analyzer_type}"
            )

        logger.debug(f"Creating turn analyzer: type={analyzer_type}, params={params}")

        return LocalSmartTurnAnalyzer(**params)
