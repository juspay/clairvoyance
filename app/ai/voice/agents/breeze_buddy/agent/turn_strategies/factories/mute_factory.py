"""Factory for creating user mute strategies."""

from typing import List

from pipecat.turns.user_mute import (
    AlwaysUserMuteStrategy,
    BaseUserMuteStrategy,
    FirstSpeechUserMuteStrategy,
    FunctionCallUserMuteStrategy,
    MuteUntilFirstBotCompleteUserMuteStrategy,
)

from app.ai.voice.agents.breeze_buddy.agent.turn_strategies.config import (
    MuteStrategyConfig,
    MuteStrategyType,
)
from app.core.logger import logger


class MuteStrategyFactory:
    """Factory for creating user mute strategy instances."""

    @staticmethod
    def create(config: MuteStrategyConfig) -> BaseUserMuteStrategy:
        """Create a mute strategy from configuration.

        Args:
            config: Mute strategy configuration

        Returns:
            Instantiated mute strategy

        Raises:
            ValueError: If strategy type is unknown
        """
        logger.debug(f"Creating mute strategy: type={config.type}")

        if config.type == MuteStrategyType.ALWAYS:
            return MuteStrategyFactory.create_always_mute()

        elif config.type == MuteStrategyType.FIRST_SPEECH:
            return MuteStrategyFactory.create_first_speech_mute()

        elif config.type == MuteStrategyType.FUNCTION_CALL:
            return MuteStrategyFactory.create_function_call_mute()

        elif config.type == MuteStrategyType.MUTE_UNTIL_FIRST_BOT_COMPLETE:
            return MuteStrategyFactory.create_mute_until_first_bot_complete()

        else:
            raise ValueError(f"Unknown mute strategy type: {config.type}")

    @staticmethod
    def create_multiple(
        configs: List[MuteStrategyConfig],
    ) -> List[BaseUserMuteStrategy]:
        """Create multiple mute strategies from configurations.

        Args:
            configs: List of mute strategy configurations

        Returns:
            List of instantiated mute strategies
        """
        strategies = []
        for config in configs:
            try:
                strategy = MuteStrategyFactory.create(config)
                strategies.append(strategy)
            except Exception as e:
                logger.error(
                    f"Failed to create mute strategy {config.type}: {e}. Skipping."
                )
        return strategies

    @staticmethod
    def create_always_mute() -> AlwaysUserMuteStrategy:
        """Create always mute strategy.

        Mutes user whenever bot is speaking.

        Returns:
            Always mute strategy instance
        """
        return AlwaysUserMuteStrategy()

    @staticmethod
    def create_first_speech_mute() -> FirstSpeechUserMuteStrategy:
        """Create first speech mute strategy.

        Mutes user only during bot's first speech, then allows interruptions.

        Returns:
            First speech mute strategy instance
        """
        return FirstSpeechUserMuteStrategy()

    @staticmethod
    def create_function_call_mute() -> FunctionCallUserMuteStrategy:
        """Create function call mute strategy.

        Mutes user while LLM function/tool calls are executing.

        Returns:
            Function call mute strategy instance
        """
        return FunctionCallUserMuteStrategy()

    @staticmethod
    def create_mute_until_first_bot_complete() -> (
        MuteUntilFirstBotCompleteUserMuteStrategy
    ):
        """Create mute until first bot complete strategy.

        Mutes user until bot completes its first turn.

        Returns:
            Mute until first bot complete strategy instance
        """
        return MuteUntilFirstBotCompleteUserMuteStrategy()
