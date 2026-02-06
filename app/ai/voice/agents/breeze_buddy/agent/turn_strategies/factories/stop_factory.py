"""Factory for creating turn stop strategies."""

from typing import List, Optional

from pipecat.turns.user_stop import (
    BaseUserTurnStopStrategy,
    ExternalUserTurnStopStrategy,
    TranscriptionUserTurnStopStrategy,
    TurnAnalyzerUserTurnStopStrategy,
)

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    StopStrategyConfig,
    StopStrategyType,
)
from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.factories.analyzer_factory import (
    TurnAnalyzerFactory,
)
from app.core.logger import logger


class StopStrategyFactory:
    """Factory for creating turn stop strategy instances."""

    @staticmethod
    def create(config: StopStrategyConfig) -> BaseUserTurnStopStrategy:
        """Create a stop strategy from configuration.

        Args:
            config: Stop strategy configuration

        Returns:
            Instantiated stop strategy

        Raises:
            ValueError: If strategy type is unknown or configuration is invalid
            ImportError: If required dependencies are not available
        """
        logger.debug(
            f"Creating stop strategy: type={config.type}, params={config.params}"
        )

        if config.type == StopStrategyType.TRANSCRIPTION:
            return StopStrategyFactory.create_transcription_strategy(**config.params)

        elif config.type == StopStrategyType.TURN_ANALYZER:
            return StopStrategyFactory.create_turn_analyzer_strategy(**config.params)

        elif config.type == StopStrategyType.EXTERNAL:
            return StopStrategyFactory.create_external_strategy(**config.params)

        else:
            raise ValueError(f"Unknown stop strategy type: {config.type}")

    @staticmethod
    def create_multiple(
        configs: List[StopStrategyConfig],
    ) -> List[BaseUserTurnStopStrategy]:
        """Create multiple stop strategies from configurations.

        Args:
            configs: List of stop strategy configurations

        Returns:
            List of instantiated stop strategies
        """
        strategies = []
        for config in configs:
            try:
                strategy = StopStrategyFactory.create(config)
                strategies.append(strategy)
            except Exception as e:
                logger.error(
                    f"Failed to create stop strategy {config.type}: {e}. Skipping."
                )
        return strategies

    @staticmethod
    def create_transcription_strategy(
        timeout: float = 0.5,
        enable_user_speaking_frames: bool = True,
        **kwargs,
    ) -> TranscriptionUserTurnStopStrategy:
        """Create transcription-based stop strategy.

        Args:
            timeout: Time to wait for transcriptions after VAD stop
            enable_user_speaking_frames: Whether to emit user speaking frames
            **kwargs: Additional parameters for the strategy

        Returns:
            Transcription stop strategy instance
        """
        return TranscriptionUserTurnStopStrategy(
            timeout=timeout,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )

    @staticmethod
    def create_turn_analyzer_strategy(
        analyzer: str,
        timeout: float = 0.5,
        analyzer_params: Optional[dict] = None,
        enable_user_speaking_frames: bool = True,
        **kwargs,
    ) -> TurnAnalyzerUserTurnStopStrategy:
        """Create turn analyzer-based stop strategy (smart turn).

        Args:
            analyzer: Type of turn analyzer to use (e.g., "local_smart_turn_v3")
            timeout: Time to wait for transcriptions after analysis
            analyzer_params: Parameters for the turn analyzer
            enable_user_speaking_frames: Whether to emit user speaking frames
            **kwargs: Additional parameters for the strategy

        Returns:
            Turn analyzer stop strategy instance

        Raises:
            ImportError: If analyzer dependencies are not available
            ValueError: If analyzer type is unknown or invalid
        """
        analyzer_params = analyzer_params or {}

        # Create the turn analyzer
        turn_analyzer = TurnAnalyzerFactory.create(analyzer, analyzer_params)

        return TurnAnalyzerUserTurnStopStrategy(
            turn_analyzer=turn_analyzer,
            timeout=timeout,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )

    @staticmethod
    def create_external_strategy(
        timeout: float = 0.5,
        enable_user_speaking_frames: bool = False,
        **kwargs,
    ) -> ExternalUserTurnStopStrategy:
        """Create external control stop strategy.

        Args:
            timeout: Time to wait for transcriptions
            enable_user_speaking_frames: Whether to emit user speaking frames (default: False for external)
            **kwargs: Additional parameters for the strategy

        Returns:
            External stop strategy instance
        """
        return ExternalUserTurnStopStrategy(
            timeout=timeout,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )
