"""
Interruption Configuration Utilities — Phase 2: Node-Level Switching

This module handles dynamic interruption strategy switching during node transitions:
1. Storing the default (template-level) interruption config on the bot
2. Resetting interruption strategies to template defaults on node exit
3. Applying node-specific interruption overrides on node entry

Mirrors the pattern established by vad.py for VAD config management.

The user_speech_timeout parameter controls how long Pipecat waits after the
last finalized transcript before ending the user's turn. Default is 0.0
(immediate). Input collection nodes override this to accumulate multi-segment
input (see template/input_collection.py).

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


async def reset_interruption_to_default(
    context: TemplateContext,
    user_speech_timeout: float = 0.0,
):
    """Reset interruption strategies to the template-level default config.

    Called at the start of every node transition (before applying node overrides),
    mirroring reset_vad_to_default().

    Args:
        context: Template context with bot state access
        user_speech_timeout: The target node's user_speech_timeout from input
            collection config. Passed here so the reset installs the correct
            timeout immediately, avoiding a race window where timeout=0.0 could
            cause premature turn ends if transcripts arrive between reset and apply.
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
        user_speech_timeout=user_speech_timeout,
    )


async def apply_node_interruption_config(
    context: TemplateContext,
    node_name: str,
    user_speech_timeout: float = 0.0,
):
    """Apply node-specific interruption config if it exists.

    Called after reset_interruption_to_default() during a node transition,
    mirroring apply_node_vad_config().

    Args:
        context: Template context with bot state access
        node_name: Name of the target node
        user_speech_timeout: Seconds to wait after last finalized transcript
            before ending user turn. Passed from input_collection config.
            Default 0.0 (immediate) for non-collection nodes.
    """
    bot = context.bot
    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        # No node config, but we may still need to apply user_speech_timeout
        # from input_collection even without an interruption override
        if user_speech_timeout > 0.0:
            user_aggregator = _get_user_aggregator(bot)
            if user_aggregator is None:
                return
            # Re-apply the current (default) interruption config with new timeout
            active_config = getattr(bot, "_active_interruption_config", None)
            config = (
                active_config if active_config is not None else InterruptionConfig()
            )
            await _apply_interruption_config(
                user_aggregator,
                config,
                has_vad=bot.vad_analyzer is not None,
                call_sid=context.call_sid or "unknown",
                label=f"node:{node_name}",
                bot=bot,
                user_speech_timeout=user_speech_timeout,
            )
        return

    node_config = nodes[node_name]
    interruption_config = node_config.get("interruption")

    # If no interruption override but user_speech_timeout changed, still apply
    if interruption_config is None and user_speech_timeout <= 0.0:
        return

    if interruption_config is not None:
        # interruption_config may be an InterruptionConfig object or a dict
        if isinstance(interruption_config, dict):
            interruption_config = InterruptionConfig.model_validate(interruption_config)
    else:
        # No interruption override — use current active config with new timeout
        active_config = getattr(bot, "_active_interruption_config", None)
        interruption_config = (
            active_config if active_config is not None else InterruptionConfig()
        )

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
        user_speech_timeout=user_speech_timeout,
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


def _config_matches_active(
    bot, config: InterruptionConfig, user_speech_timeout: float
) -> bool:
    """Check if the given config matches the currently active one."""
    active = getattr(bot, "_active_interruption_config", None)
    active_timeout = getattr(bot, "_active_user_speech_timeout", None)
    if active is None:
        return False
    return (
        active.mode == config.mode
        and active.min_words == config.min_words
        and active_timeout == user_speech_timeout
    )


async def _apply_interruption_config(
    user_aggregator,
    config: InterruptionConfig,
    has_vad: bool,
    call_sid: str,
    label: str,
    bot=None,
    user_speech_timeout: float = 0.0,
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
        user_speech_timeout: Seconds to wait after last finalized transcript
            before ending user turn. Default 0.0 (immediate). Set by input
            collection config for multi-segment input nodes.
    """
    # Short-circuit when the requested config is already active
    if bot is not None and _config_matches_active(bot, config, user_speech_timeout):
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

    # pipecat 1.1.0's SpeechTimeoutUserTurnStopStrategy handles the no-VAD
    # multi-turn case natively (per-turn reset + independent wait flags), so the
    # turn ends after user_speech_timeout seconds of silence following the last
    # transcript. user_speech_timeout=0 fires immediately on a finalized transcript.
    new_strategies = UserTurnStrategies(
        start=start_strategies,
        stop=[
            SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=user_speech_timeout)
        ],
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
        bot._active_user_speech_timeout = user_speech_timeout

    logger.info(
        f"Interruption config applied [{label}] for call {call_sid}: "
        f"mode={config.mode.value}, min_words={config.min_words}, "
        f"user_speech_timeout={user_speech_timeout}s, "
        f"mute_strategies={len(old_mute)}"
    )
