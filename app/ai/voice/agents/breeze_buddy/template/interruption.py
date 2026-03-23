"""
Interruption Configuration Utilities — Phase 2: Node-Level Switching

This module handles dynamic interruption strategy switching during node transitions:
1. Storing the default (template-level) interruption config on the bot
2. Resetting interruption strategies to template defaults on node exit
3. Applying node-specific interruption overrides on node entry

Mirrors the pattern established by vad.py for VAD config management.

For template-level (Phase 1) interruption wiring at pipeline creation time,
see agent/pipeline.py.
"""

from pipecat.turns.user_mute import AlwaysUserMuteStrategy
from pipecat.turns.user_start import (
    BaseUserTurnStartStrategy,
    MinWordsUserTurnStartStrategy,
    TranscriptionUserTurnStartStrategy,
    VADUserTurnStartStrategy,
)
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    InterruptionConfig,
    InterruptionMode,
)
from app.core.logger import logger


async def reset_interruption_to_default(context: TemplateContext):
    """Reset interruption strategies to the template-level default config.

    Called at the start of every node transition (before applying node overrides),
    mirroring reset_vad_to_default().
    """
    bot = context.bot
    default_config = getattr(bot, "default_interruption_config", None)
    if default_config is None:
        return

    user_aggregator = _get_user_aggregator(bot)
    if user_aggregator is None:
        return

    await _apply_interruption_config(
        user_aggregator,
        default_config,
        has_vad=bot.vad_analyzer is not None,
        call_sid=context.call_sid or "unknown",
        label="default",
        bot=bot,
    )


async def apply_node_interruption_config(context: TemplateContext, node_name: str):
    """Apply node-specific interruption config if it exists.

    Called after reset_interruption_to_default() during a node transition,
    mirroring apply_node_vad_config().
    """
    bot = context.bot
    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        return

    node_config = nodes[node_name]
    interruption_config = node_config.get("interruption")
    if interruption_config is None:
        return

    # interruption_config may be an InterruptionConfig object or a dict
    if isinstance(interruption_config, dict):
        interruption_config = InterruptionConfig.model_validate(interruption_config)

    user_aggregator = _get_user_aggregator(bot)
    if user_aggregator is None:
        return

    await _apply_interruption_config(
        user_aggregator,
        interruption_config,
        has_vad=bot.vad_analyzer is not None,
        call_sid=context.call_sid or "unknown",
        label=f"node:{node_name}",
        bot=bot,
    )


def _get_user_aggregator(bot):
    """Get the LLMUserAggregator from the bot's context aggregator."""
    aggregator_pair = getattr(bot, "_context_aggregator", None)
    if aggregator_pair is None:
        logger.warning(
            "No context aggregator on bot; cannot switch interruption config"
        )
        return None
    return aggregator_pair.user()


def _config_matches_active(bot, config: InterruptionConfig) -> bool:
    """Check if the given config matches the currently active one."""
    active = getattr(bot, "_active_interruption_config", None)
    if active is None:
        return False
    return active.mode == config.mode and active.min_words == config.min_words


async def _apply_interruption_config(
    user_aggregator,
    config: InterruptionConfig,
    has_vad: bool,
    call_sid: str,
    label: str,
    bot=None,
):
    """Build and apply interruption strategies from an InterruptionConfig.

    This updates both:
    1. Turn start strategies via UserTurnController.update_strategies()
    2. Mute strategies via direct _params.user_mute_strategies manipulation

    Skips the update if the requested config already matches the active one.

    Args:
        user_aggregator: LLMUserAggregator instance from the pipeline
        config: The InterruptionConfig to apply
        has_vad: Whether a VAD analyzer is active
        call_sid: Call SID for logging
        label: Label for log messages (e.g., "default", "node:greeting")
        bot: Bot instance for tracking active config (skip-if-unchanged)
    """
    # Short-circuit when the requested config is already active
    if bot is not None and _config_matches_active(bot, config):
        logger.debug(
            f"Interruption config unchanged [{label}] for call {call_sid}, skipping"
        )
        return

    # --- 1. Rebuild and update turn start/stop strategies ---
    start_strategies: list[BaseUserTurnStartStrategy] = []
    if has_vad:
        start_strategies.append(VADUserTurnStartStrategy())

    if config.min_words and config.mode == InterruptionMode.ENABLED:
        start_strategies.append(
            MinWordsUserTurnStartStrategy(min_words=config.min_words, use_interim=True)
        )
    else:
        start_strategies.append(TranscriptionUserTurnStartStrategy(use_interim=True))

    new_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=0.0)],
    )

    await user_aggregator._user_turn_controller.update_strategies(new_strategies)

    # --- 2. Update mute strategies ---
    old_mute = user_aggregator._params.user_mute_strategies

    # Cleanup existing mute strategies
    for s in old_mute:
        await s.cleanup()
    old_mute.clear()

    if config.mode == InterruptionMode.DISABLED_DISCARD:
        mute_strategy = AlwaysUserMuteStrategy()
        await mute_strategy.setup(user_aggregator.task_manager)
        old_mute.append(mute_strategy)
    else:
        # When switching from muted → unmuted, clear the mute flag so frames
        # aren't suppressed until the next process_frame cycle naturally clears it.
        user_aggregator._user_is_muted = False

    # Track the active config so subsequent calls can short-circuit
    if bot is not None:
        bot._active_interruption_config = config

    logger.info(
        f"Interruption config applied [{label}] for call {call_sid}: "
        f"mode={config.mode.value}, min_words={config.min_words}, "
        f"mute_strategies={len(old_mute)}"
    )
