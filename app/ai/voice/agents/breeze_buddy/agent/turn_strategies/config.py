"""Configuration models for turn strategies."""

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class StartStrategyType(str, Enum):
    """Turn start strategy types."""

    VAD = "vad"
    TRANSCRIPTION = "transcription"
    MIN_WORDS = "min_words"
    EXTERNAL = "external"


class StopStrategyType(str, Enum):
    """Turn stop strategy types."""

    TRANSCRIPTION = "transcription"
    TURN_ANALYZER = "turn_analyzer"
    EXTERNAL = "external"


class TurnAnalyzerType(str, Enum):
    """Turn analyzer types for turn_analyzer stop strategy."""

    LOCAL_SMART_TURN_V3 = "local_smart_turn_v3"
    LOCAL_SMART_TURN_V2 = "local_smart_turn_v2"
    LOCAL_SMART_TURN_V1 = "local_smart_turn_v1"
    LOCAL_COREML = "local_coreml"
    FAL = "fal"
    KRISP_VIVA = "krisp_viva"


class MuteStrategyType(str, Enum):
    """User mute strategy types."""

    ALWAYS = "always"
    FIRST_SPEECH = "first_speech"
    FUNCTION_CALL = "function_call"
    MUTE_UNTIL_FIRST_BOT_COMPLETE = "mute_until_first_bot_complete"


class StartStrategyConfig(BaseModel):
    """Configuration for a turn start strategy.

    Examples:
        VAD-based:
            {"type": "vad", "params": {}}

        Min words:
            {"type": "min_words", "params": {"min_words": 3, "use_interim": true}}

        Transcription-based:
            {"type": "transcription", "params": {"use_interim": true}}
    """

    type: StartStrategyType
    params: Dict[str, Any] = Field(
        default_factory=dict, description="Strategy-specific parameters"
    )

    @field_validator("params")
    @classmethod
    def validate_params(cls, v: Dict[str, Any], info) -> Dict[str, Any]:
        """Validate params based on strategy type."""
        strategy_type = info.data.get("type")

        if strategy_type == StartStrategyType.MIN_WORDS:
            if "min_words" in v and not isinstance(v["min_words"], int):
                raise ValueError("min_words must be an integer")
            if "min_words" in v and v["min_words"] < 1:
                raise ValueError("min_words must be at least 1")

        if strategy_type == StartStrategyType.TRANSCRIPTION:
            if "use_interim" in v and not isinstance(v["use_interim"], bool):
                raise ValueError("use_interim must be a boolean")

        return v


class StopStrategyConfig(BaseModel):
    """Configuration for a turn stop strategy.

    Examples:
        Transcription-based:
            {"type": "transcription", "params": {"timeout": 0.5}}

        Turn analyzer (smart turn):
            {
                "type": "turn_analyzer",
                "params": {
                    "analyzer": "local_smart_turn_v3",
                    "timeout": 0.5,
                    "analyzer_params": {}
                }
            }

        External control:
            {"type": "external", "params": {"timeout": 0.5}}
    """

    type: StopStrategyType
    params: Dict[str, Any] = Field(
        default_factory=dict, description="Strategy-specific parameters"
    )

    @field_validator("params")
    @classmethod
    def validate_params(cls, v: Dict[str, Any], info) -> Dict[str, Any]:
        """Validate params based on strategy type."""
        strategy_type = info.data.get("type")

        # Validate timeout for all strategies
        if "timeout" in v:
            if not isinstance(v["timeout"], (int, float)):
                raise ValueError("timeout must be a number")
            if v["timeout"] <= 0:
                raise ValueError("timeout must be positive")

        # Validate turn_analyzer specific params
        if strategy_type == StopStrategyType.TURN_ANALYZER:
            if "analyzer" not in v:
                raise ValueError("turn_analyzer strategy requires 'analyzer' parameter")

            analyzer = v["analyzer"]
            if isinstance(analyzer, str):
                try:
                    TurnAnalyzerType(analyzer)
                except ValueError:
                    valid_types = [t.value for t in TurnAnalyzerType]
                    raise ValueError(
                        f"Invalid analyzer type '{analyzer}'. "
                        f"Must be one of: {', '.join(valid_types)}"
                    )

            # Validate analyzer_params if provided
            if "analyzer_params" in v and not isinstance(v["analyzer_params"], dict):
                raise ValueError("analyzer_params must be a dictionary")

        return v


class MuteStrategyConfig(BaseModel):
    """Configuration for a user mute strategy.

    Examples:
        Always mute while bot speaks:
            {"type": "always"}

        Mute only during first bot speech:
            {"type": "first_speech"}

        Mute during function calls:
            {"type": "function_call"}
    """

    type: MuteStrategyType


class TurnStrategyConfigModel(BaseModel):
    """Complete turn strategy configuration for a template.

    This model defines all turn-taking behavior including when to start/stop
    user turns and when to mute user input.

    Examples:
        Simple smart turn:
            {
                "enabled": true,
                "stop_strategies": [{
                    "type": "turn_analyzer",
                    "params": {"analyzer": "local_smart_turn_v3", "timeout": 0.5}
                }]
            }

        Prevent false starts with smart turn:
            {
                "enabled": true,
                "start_strategies": [{
                    "type": "min_words",
                    "params": {"min_words": 3}
                }],
                "stop_strategies": [{
                    "type": "turn_analyzer",
                    "params": {"analyzer": "local_smart_turn_v3"}
                }],
                "user_turn_stop_timeout": 7.0
            }

        With muting during function calls:
            {
                "enabled": true,
                "stop_strategies": [{
                    "type": "turn_analyzer",
                    "params": {"analyzer": "local_smart_turn_v3"}
                }],
                "mute_strategies": [{"type": "function_call"}]
            }
    """

    enabled: bool = Field(
        default=False, description="Whether to use custom turn strategies"
    )

    start_strategies: Optional[List[StartStrategyConfig]] = Field(
        default=None,
        description="Turn start strategies (defaults to VAD + Transcription if None)",
    )

    stop_strategies: Optional[List[StopStrategyConfig]] = Field(
        default=None,
        description="Turn stop strategies (defaults to Transcription if None)",
    )

    mute_strategies: Optional[List[MuteStrategyConfig]] = Field(
        default=None, description="User mute strategies (defaults to no muting if None)"
    )

    user_turn_stop_timeout: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Safety timeout in seconds to force-stop user turn",
    )

    @field_validator("start_strategies")
    @classmethod
    def validate_start_strategies(
        cls, v: Optional[List[StartStrategyConfig]]
    ) -> Optional[List[StartStrategyConfig]]:
        """Validate start strategies list."""
        if v is not None and len(v) == 0:
            raise ValueError(
                "start_strategies must contain at least one strategy if provided"
            )
        return v

    @field_validator("stop_strategies")
    @classmethod
    def validate_stop_strategies(
        cls, v: Optional[List[StopStrategyConfig]]
    ) -> Optional[List[StopStrategyConfig]]:
        """Validate stop strategies list."""
        if v is not None and len(v) == 0:
            raise ValueError(
                "stop_strategies must contain at least one strategy if provided"
            )
        if v is not None and len(v) > 1:
            raise ValueError("Only one stop strategy is supported at this time")
        return v

    @field_validator("mute_strategies")
    @classmethod
    def validate_mute_strategies(
        cls, v: Optional[List[MuteStrategyConfig]]
    ) -> Optional[List[MuteStrategyConfig]]:
        """Validate mute strategies list."""
        if v is not None and len(v) == 0:
            raise ValueError(
                "mute_strategies must contain at least one strategy if provided"
            )
        return v
