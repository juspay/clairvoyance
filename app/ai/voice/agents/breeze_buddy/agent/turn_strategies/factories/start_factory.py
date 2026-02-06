"""Factory for creating turn start strategies."""

from typing import List

from pipecat.turns.user_start import (
    BaseUserTurnStartStrategy,
    ExternalUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    StartStrategyConfig,
    StartStrategyType,
)
from app.core.logger import logger


class StartStrategyFactory:
    """Factory for creating turn start strategy instances."""

    @staticmethod
    def create(config: StartStrategyConfig) -> BaseUserTurnStartStrategy:
        """Create a start strategy from configuration.

        Args:
            config: Start strategy configuration

        Returns:
            Instantiated start strategy

        Raises:
            ValueError: If strategy type is unknown or configuration is invalid
        """
        logger.debug(
            f"Creating start strategy: type={config.type}, params={config.params}"
        )

        if config.type == StartStrategyType.VAD:
            return StartStrategyFactory.create_vad_strategy(**config.params)

        elif config.type == StartStrategyType.TRANSCRIPTION:
            return StartStrategyFactory.create_transcription_strategy(**config.params)

        elif config.type == StartStrategyType.MIN_WORDS:
            return StartStrategyFactory.create_min_words_strategy(**config.params)

        elif config.type == StartStrategyType.EXTERNAL:
            return StartStrategyFactory.create_external_strategy(**config.params)

        else:
            raise ValueError(f"Unknown start strategy type: {config.type}")

    @staticmethod
    def create_multiple(
        configs: List[StartStrategyConfig],
    ) -> List[BaseUserTurnStartStrategy]:
        """Create multiple start strategies from configurations.

        Args:
            configs: List of start strategy configurations

        Returns:
            List of instantiated start strategies
        """
        strategies = []
        for config in configs:
            try:
                strategy = StartStrategyFactory.create(config)
                strategies.append(strategy)
            except Exception as e:
                logger.error(
                    f"Failed to create start strategy {config.type}: {e}. Skipping."
                )
        return strategies

    @staticmethod
    def create_vad_strategy(
        enable_interruptions: bool = True,
        enable_user_speaking_frames: bool = True,
        **kwargs,
    ) -> VADUserTurnStartStrategy:
        """Create VAD-based start strategy.

        Args:
            enable_interruptions: Whether to allow interruptions during bot speech
            enable_user_speaking_frames: Whether to emit user speaking frames
            **kwargs: Additional parameters for the strategy

        Returns:
            VAD start strategy instance
        """
        return VADUserTurnStartStrategy(
            enable_interruptions=enable_interruptions,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )

    @staticmethod
    def create_transcription_strategy(
        use_interim: bool = True,
        enable_interruptions: bool = True,
        enable_user_speaking_frames: bool = True,
        **kwargs,
    ) -> TranscriptionUserTurnStartStrategy:
        """Create transcription-based start strategy.

        Args:
            use_interim: Whether to use interim transcriptions
            enable_interruptions: Whether to allow interruptions during bot speech
            enable_user_speaking_frames: Whether to emit user speaking frames
            **kwargs: Additional parameters for the strategy

        Returns:
            Transcription start strategy instance
        """
        return TranscriptionUserTurnStartStrategy(
            use_interim=use_interim,
            enable_interruptions=enable_interruptions,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )

    @staticmethod
    def create_min_words_strategy(
        min_words: int = 3,
        use_interim: bool = True,
        enable_interruptions: bool = True,
        enable_user_speaking_frames: bool = True,
        **kwargs,
    ) -> MinWordsUserTurnStartStrategy:
        """Create minimum words start strategy.

        Args:
            min_words: Minimum number of words to trigger start
            use_interim: Whether to use interim transcriptions
            enable_interruptions: Whether to allow interruptions during bot speech
            enable_user_speaking_frames: Whether to emit user speaking frames
            **kwargs: Additional parameters for the strategy

        Returns:
            Minimum words start strategy instance
        """
        return MinWordsUserTurnStartStrategy(
            min_words=min_words,
            use_interim=use_interim,
            enable_interruptions=enable_interruptions,
            enable_user_speaking_frames=enable_user_speaking_frames,
            **kwargs,
        )

    @staticmethod
    def create_external_strategy(**kwargs) -> ExternalUserTurnStartStrategy:
        """Create external control start strategy.

        Args:
            **kwargs: Additional parameters for the strategy

        Returns:
            External start strategy instance
        """
        return ExternalUserTurnStartStrategy(**kwargs)
