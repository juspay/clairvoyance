"""
Modular turn strategy system for Breeze Buddy.

This package provides a factory-based approach to creating and configuring
turn strategies from template JSON configurations.
"""

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.builder import (
    build_turn_strategies_from_config,
    build_turn_strategies_from_template,
)
from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    MuteStrategyConfig,
    MuteStrategyType,
    StartStrategyConfig,
    StartStrategyType,
    StopStrategyConfig,
    StopStrategyType,
    TurnAnalyzerType,
    TurnStrategyConfigModel,
)

__all__ = [
    # Builder functions
    "build_turn_strategies_from_config",
    "build_turn_strategies_from_template",
    # Config models
    "TurnStrategyConfigModel",
    "StartStrategyConfig",
    "StopStrategyConfig",
    "MuteStrategyConfig",
    # Enums
    "StartStrategyType",
    "StopStrategyType",
    "TurnAnalyzerType",
    "MuteStrategyType",
]
