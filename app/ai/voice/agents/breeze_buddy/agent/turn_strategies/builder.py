"""Builder for creating complete turn strategy configurations from templates."""

from typing import Optional, Tuple

from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    TurnStrategyConfigModel,
)
from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.factories import (
    MuteStrategyFactory,
    StartStrategyFactory,
    StopStrategyFactory,
)
from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.validators import (
    validate_turn_strategy_config,
)
from app.core.logger import logger


async def build_turn_strategies_from_template(
    template,
    is_daily_mode: bool = False,
) -> Tuple[Optional[UserTurnStrategies], float]:
    """Build turn strategies from template configuration.

    Args:
        template: Template model instance
        is_daily_mode: Whether this is Daily mode (unused currently)

    Returns:
        Tuple of (UserTurnStrategies or None, user_turn_stop_timeout)
        - If strategies configured: (UserTurnStrategies instance, configured timeout)
        - If not configured: (None, default timeout 5.0)
    """
    # Try to get turn strategy config from template
    turn_strategy_config = None
    if template and hasattr(template, "configurations") and template.configurations:
        turn_strategy_config = getattr(
            template.configurations, "turn_strategy_config", None
        )

    # If template has turn strategy config and it's enabled, use it
    if turn_strategy_config and turn_strategy_config.enabled:
        logger.info("Building turn strategies from template configuration")
        return await build_turn_strategies_from_config(turn_strategy_config)

    # No turn strategies configured, use pipecat defaults
    logger.debug("No turn strategies configured, using pipecat defaults")
    return None, 5.0


async def build_turn_strategies_from_config(
    config: TurnStrategyConfigModel,
) -> Tuple[Optional[UserTurnStrategies], float]:
    """Build turn strategies from configuration model.

    Args:
        config: Turn strategy configuration model

    Returns:
        Tuple of (UserTurnStrategies or None, user_turn_stop_timeout)

    Raises:
        ValueError: If configuration is invalid
        ImportError: If required dependencies are not available
    """
    # Validate configuration
    is_valid, error_msg = validate_turn_strategy_config(config)
    if not is_valid:
        raise ValueError(f"Invalid turn strategy configuration: {error_msg}")

    if not config.enabled:
        logger.debug("Turn strategies disabled in configuration")
        return None, config.user_turn_stop_timeout

    # Build start strategies
    start_strategies = None
    if config.start_strategies:
        logger.info(f"Creating {len(config.start_strategies)} start strategies")
        start_strategies = StartStrategyFactory.create_multiple(config.start_strategies)
        if not start_strategies:
            logger.warning("No start strategies could be created, using defaults")
            start_strategies = None

    # Build stop strategies
    stop_strategies = None
    if config.stop_strategies:
        logger.info(f"Creating {len(config.stop_strategies)} stop strategies")
        stop_strategies = StopStrategyFactory.create_multiple(config.stop_strategies)
        if not stop_strategies:
            logger.warning("No stop strategies could be created, using defaults")
            stop_strategies = None

    # Build user turn strategies
    user_turn_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=stop_strategies,
    )

    # Build mute strategies (passed separately to LLMUserAggregatorParams)
    # Note: We return them as part of the tuple if needed, but for now just log
    if config.mute_strategies:
        logger.info(
            f"Configuration includes {len(config.mute_strategies)} mute strategies"
        )
        mute_strategies = MuteStrategyFactory.create_multiple(config.mute_strategies)
        logger.debug(f"Created {len(mute_strategies)} mute strategy instances")
        # TODO: Return mute strategies when we update LLMUserAggregatorParams integration

    logger.info(
        f"Turn strategies built successfully with timeout={config.user_turn_stop_timeout}s"
    )

    return user_turn_strategies, config.user_turn_stop_timeout


def get_default_turn_strategy_config() -> TurnStrategyConfigModel:
    """Get default turn strategy configuration.

    Returns:
        Default configuration with smart turn disabled
    """
    return TurnStrategyConfigModel(
        enabled=False,
        start_strategies=None,  # Will use pipecat defaults
        stop_strategies=None,  # Will use pipecat defaults
        mute_strategies=None,  # No muting
        user_turn_stop_timeout=5.0,
    )
