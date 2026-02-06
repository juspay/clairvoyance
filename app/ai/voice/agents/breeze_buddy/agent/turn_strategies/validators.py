"""Validation utilities for turn strategy configurations."""

from typing import Optional

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    TurnAnalyzerType,
    TurnStrategyConfigModel,
)
from app.core.logger import logger


class TurnStrategyValidationError(Exception):
    """Raised when turn strategy configuration is invalid."""


def validate_turn_strategy_config(
    config: TurnStrategyConfigModel,
) -> tuple[bool, Optional[str]]:
    """Validate a turn strategy configuration.

    Args:
        config: Turn strategy configuration to validate

    Returns:
        Tuple of (is_valid, error_message)
        - is_valid: True if configuration is valid
        - error_message: Error description if invalid, None if valid
    """
    try:
        # Config model validation happens via Pydantic validators
        # Additional semantic validation can be added here

        # Check if enabled but no strategies specified
        if config.enabled:
            has_start = (
                config.start_strategies is not None and len(config.start_strategies) > 0
            )
            has_stop = (
                config.stop_strategies is not None and len(config.stop_strategies) > 0
            )

            # It's okay to only specify stop strategies (start uses defaults)
            # But if specifying start, should probably specify stop too
            if has_start and not has_stop:
                logger.warning(
                    "Turn strategy config has start strategies but no stop strategies. "
                    "Will use default transcription-based stop strategy."
                )

        return True, None

    except Exception as e:
        return False, str(e)


def validate_analyzer_availability(analyzer_type: str) -> tuple[bool, Optional[str]]:
    """Check if a turn analyzer type is available in the environment.

    Args:
        analyzer_type: The analyzer type to check (only "local_smart_turn_v3" supported)

    Returns:
        Tuple of (is_available, error_message)
        - is_available: True if analyzer can be imported
        - error_message: Import error if unavailable, None if available
    """
    try:
        if analyzer_type == TurnAnalyzerType.LOCAL_SMART_TURN_V3:
            # Import check for LocalSmartTurnAnalyzerV3
            from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import (  # noqa: F401
                LocalSmartTurnAnalyzerV3,
            )

            return True, None
        else:
            return (
                False,
                f"Only '{TurnAnalyzerType.LOCAL_SMART_TURN_V3}' analyzer is supported. Got: {analyzer_type}",
            )

    except ImportError as e:
        error_msg = (
            f"Analyzer '{analyzer_type}' is not available. "
            f"Install required dependencies: pip install 'pipecat-ai[local-smart-turn-v3]'. "
            f"Error: {str(e)}"
        )
        return False, error_msg


def get_missing_dependencies_message(analyzer_type: str) -> str:
    """Get installation instructions for missing analyzer dependencies.

    Args:
        analyzer_type: The analyzer type that is missing

    Returns:
        Human-readable installation instructions
    """
    if analyzer_type == TurnAnalyzerType.LOCAL_SMART_TURN_V3:
        command = "pip install 'pipecat-ai[local-smart-turn-v3]'"
    else:
        command = "pip install pipecat-ai"

    return f"""
Turn analyzer '{analyzer_type}' is not available.

To use this analyzer, install the required dependencies:
    {command}

Note: Only 'local_smart_turn_v3' analyzer is supported in Breeze Buddy.
"""
