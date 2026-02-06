"""Turn strategy configuration for voice agents."""

from typing import Optional

from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.services.local_smart_turn import (
    LocalSmartTurnAnalyzer,
)

__all__ = ["create_turn_strategies"]


def create_turn_strategies(
    enable_smart_turn: bool = True,
    smart_turn_timeout: float = 0.5,
) -> Optional[UserTurnStrategies]:
    """Create user turn strategies with smart turn analyzer.

    Args:
        enable_smart_turn: Whether to enable smart turn detection with ML model
        smart_turn_timeout: Timeout in seconds to wait for transcription after turn analysis

    Returns:
        UserTurnStrategies instance if smart turn is enabled, None otherwise
    """
    if not enable_smart_turn:
        return None

    # Use our wrapper around LocalSmartTurnAnalyzerV3 for proper resource management
    turn_analyzer = LocalSmartTurnAnalyzer()

    return UserTurnStrategies(
        # Keep default start strategies: VAD + Transcription
        # Only customize stop strategy with turn analyzer
        stop=[
            TurnAnalyzerUserTurnStopStrategy(
                turn_analyzer=turn_analyzer,
                timeout=smart_turn_timeout,
            )
        ]
    )
