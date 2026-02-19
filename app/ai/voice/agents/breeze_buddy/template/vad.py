"""
VAD (Voice Activity Detection) Configuration Utilities

This module handles VAD parameter management for template-based voice agents:
1. Building default VAD params from template config
2. Resetting VAD params to default template-level config
3. Applying node-specific VAD configurations
4. Muting/unmuting VAD
"""

from pipecat.audio.vad.vad_analyzer import VADParams

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.config.static import (
    BREEZE_BUDDY_VAD_CONFIDENCE,
    BREEZE_BUDDY_VAD_MIN_VOLUME,
    BREEZE_BUDDY_VAD_START_SECS,
    BREEZE_BUDDY_VAD_STOP_SECS,
)
from app.core.logger import logger


def build_default_vad_params(template) -> VADParams:
    """Build default VAD params from template config, falling back to system defaults.

    Args:
        template: Template object with configurations.vad_config

    Returns:
        VADParams with values from template or system defaults
    """
    template_vad_config = (
        template.configurations.vad_config
        if template and template.configurations
        else None
    )

    logger.info(f"Template VAD config: {template_vad_config}")

    if template_vad_config:
        default_confidence = (
            template_vad_config.confidence
            if template_vad_config.confidence is not None
            else BREEZE_BUDDY_VAD_CONFIDENCE
        )
        default_start_secs = (
            template_vad_config.start_secs
            if template_vad_config.start_secs is not None
            else BREEZE_BUDDY_VAD_START_SECS
        )
        default_stop_secs = (
            template_vad_config.stop_secs
            if template_vad_config.stop_secs is not None
            else BREEZE_BUDDY_VAD_STOP_SECS
        )
        default_min_volume = (
            template_vad_config.min_volume
            if template_vad_config.min_volume is not None
            else BREEZE_BUDDY_VAD_MIN_VOLUME
        )
    else:
        default_confidence = BREEZE_BUDDY_VAD_CONFIDENCE
        default_start_secs = BREEZE_BUDDY_VAD_START_SECS
        default_stop_secs = BREEZE_BUDDY_VAD_STOP_SECS
        default_min_volume = BREEZE_BUDDY_VAD_MIN_VOLUME

    return VADParams(
        confidence=default_confidence,
        start_secs=default_start_secs,
        stop_secs=default_stop_secs,
        min_volume=default_min_volume,
    )


def reset_vad_to_default(context: TemplateContext):
    """Reset VAD params to the default template-level config."""
    bot = context.bot
    if bot.vad_analyzer and bot.default_vad_params:
        old_confidence = bot.vad_analyzer.params.confidence
        bot.vad_analyzer.set_params(
            VADParams(
                confidence=bot.default_vad_params.confidence,
                start_secs=bot.default_vad_params.start_secs,
                stop_secs=bot.default_vad_params.stop_secs,
                min_volume=bot.default_vad_params.min_volume,
            )
        )
        logger.info(
            f"VAD params reset to default for call {context.call_sid} "
            f"(confidence: {old_confidence} -> {bot.default_vad_params.confidence})"
        )


def apply_node_vad_config(context: TemplateContext, node_name: str):
    """Apply node-specific VAD config if it exists."""
    bot = context.bot
    if not bot.vad_analyzer:
        return

    # Get node from flow config
    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        return

    node_config = nodes[node_name]
    # The NodeConfig is a TypedDict, so check for vad_config as a dict key
    vad_config = node_config.get("vad_config")
    if vad_config:
        _apply_vad_config_to_analyzer(bot.vad_analyzer, vad_config, context.call_sid)


def _get_vad_config_value(vad_config, key: str):
    """Get a value from vad_config, supporting both dict and object access."""
    if isinstance(vad_config, dict):
        return vad_config.get(key)
    return getattr(vad_config, key, None)


def _apply_vad_config_to_analyzer(vad_analyzer, vad_config, call_sid: str):
    """Apply VAD config to the VAD analyzer.

    Supports both dict and object access patterns for vad_config.
    Uses set_params() to ensure internal frame counts are recalculated.
    """
    old_params = {
        "confidence": vad_analyzer.params.confidence,
        "start_secs": vad_analyzer.params.start_secs,
        "stop_secs": vad_analyzer.params.stop_secs,
        "min_volume": vad_analyzer.params.min_volume,
    }

    confidence = _get_vad_config_value(vad_config, "confidence")
    start_secs = _get_vad_config_value(vad_config, "start_secs")
    stop_secs = _get_vad_config_value(vad_config, "stop_secs")
    min_volume = _get_vad_config_value(vad_config, "min_volume")

    vad_analyzer.set_params(
        VADParams(
            confidence=(
                confidence if confidence is not None else vad_analyzer.params.confidence
            ),
            start_secs=(
                start_secs if start_secs is not None else vad_analyzer.params.start_secs
            ),
            stop_secs=(
                stop_secs if stop_secs is not None else vad_analyzer.params.stop_secs
            ),
            min_volume=(
                min_volume if min_volume is not None else vad_analyzer.params.min_volume
            ),
        )
    )

    new_params = {
        "confidence": vad_analyzer.params.confidence,
        "start_secs": vad_analyzer.params.start_secs,
        "stop_secs": vad_analyzer.params.stop_secs,
        "min_volume": vad_analyzer.params.min_volume,
    }

    logger.info(
        f"Node VAD config applied for call {call_sid}: {old_params} -> {new_params}"
    )


def mute_vad(context: TemplateContext):
    """
    Mute VAD by setting confidence to 1.0.

    Stores the previous VAD params so they can be restored when unmuting.
    """
    if context.vad_analyzer:
        # Store current VAD params before muting
        context.bot._pre_mute_vad_params = {
            "confidence": context.vad_analyzer.params.confidence,
            "start_secs": context.vad_analyzer.params.start_secs,
            "stop_secs": context.vad_analyzer.params.stop_secs,
            "min_volume": context.vad_analyzer.params.min_volume,
        }
        old_confidence = context.vad_analyzer.params.confidence
        context.vad_analyzer.set_params(
            VADParams(
                confidence=1.0,
                start_secs=context.vad_analyzer.params.start_secs,
                stop_secs=context.vad_analyzer.params.stop_secs,
                min_volume=context.vad_analyzer.params.min_volume,
            )
        )
        logger.info(
            f"STT muted via VAD for call {context.call_sid} "
            f"(confidence: {old_confidence} -> 1.0, stored pre-mute params)"
        )
        return True
    else:
        logger.warning(
            f"No VAD analyzer found for call {context.call_sid}, cannot mute STT"
        )
        return False


def unmute_vad(context: TemplateContext):
    """
    Unmute VAD by restoring the params that were stored before muting.

    Falls back to template's default VAD params or static config if no stored params exist.
    """
    if context.vad_analyzer:
        old_confidence = context.vad_analyzer.params.confidence
        bot = context.bot

        # Check if we have stored pre-mute params
        if hasattr(bot, "_pre_mute_vad_params") and bot._pre_mute_vad_params:
            stored_params = bot._pre_mute_vad_params
            context.vad_analyzer.set_params(
                VADParams(
                    confidence=stored_params["confidence"],
                    start_secs=stored_params["start_secs"],
                    stop_secs=stored_params["stop_secs"],
                    min_volume=stored_params["min_volume"],
                )
            )
            logger.info(
                f"STT unmuted via VAD for call {context.call_sid} "
                f"(restored pre-mute params: {stored_params})"
            )
            # Clear stored params after restoration
            bot._pre_mute_vad_params = None
        elif hasattr(bot, "default_vad_params") and bot.default_vad_params:
            # Fall back to template's default VAD params
            context.vad_analyzer.set_params(
                VADParams(
                    confidence=bot.default_vad_params.confidence,
                    start_secs=bot.default_vad_params.start_secs,
                    stop_secs=bot.default_vad_params.stop_secs,
                    min_volume=bot.default_vad_params.min_volume,
                )
            )
            logger.info(
                f"STT unmuted via VAD for call {context.call_sid} "
                f"(no stored params, using default: confidence {old_confidence} -> {bot.default_vad_params.confidence})"
            )
        else:
            # Fall back to static config
            context.vad_analyzer.set_params(
                VADParams(
                    confidence=BREEZE_BUDDY_VAD_CONFIDENCE,
                    start_secs=BREEZE_BUDDY_VAD_START_SECS,
                    stop_secs=BREEZE_BUDDY_VAD_STOP_SECS,
                    min_volume=BREEZE_BUDDY_VAD_MIN_VOLUME,
                )
            )
            logger.info(
                f"STT unmuted via VAD for call {context.call_sid} "
                f"(no stored/default params, using static config: confidence {old_confidence} -> {BREEZE_BUDDY_VAD_CONFIDENCE})"
            )
        return True
    else:
        logger.warning(
            f"No VAD analyzer found for call {context.call_sid}, cannot unmute STT"
        )
        return False
