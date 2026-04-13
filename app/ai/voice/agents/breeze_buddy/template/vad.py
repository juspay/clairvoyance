"""VAD (Voice Activity Detection) configuration and runtime mutation.

Covers both:
- build-time: `create_vad_analyzer` + `build_daily_vad_params` /
  `build_telephony_vad_params`, called once at agent startup to construct the
  `SileroVADAnalyzer` with template > Redis layered params.
- runtime: `reset_vad_to_default`, `apply_node_vad_config`, `mute_vad`,
  `unmute_vad`, called during a live call to mutate the analyzer in place
  (e.g. node transitions, temporary STT mute).

For the full mute_stt/unmute_stt routing (VAD → TranscriptionGate fallback),
see handlers/internal/stt.py.
"""

from typing import Optional

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.config.dynamic import (
    BB_DAILY_VAD_CONFIDENCE,
    BB_DAILY_VAD_MIN_VOLUME,
    BB_DAILY_VAD_START_SECS,
    BB_DAILY_VAD_STOP_SECS,
    BB_TELEPHONY_VAD_CONFIDENCE,
    BB_TELEPHONY_VAD_MIN_VOLUME,
    BB_TELEPHONY_VAD_START_SECS,
    BB_TELEPHONY_VAD_STOP_SECS,
    BREEZE_BUDDY_ENABLE_VAD,
)
from app.core.logger import logger

# Constants
TELEPHONY_SAMPLE_RATE = 8000
DAILY_SAMPLE_RATE = 16000


async def create_daily_vad_params() -> VADParams:
    """Create VAD parameters for Daily mode from Redis dynamic config."""
    return VADParams(
        confidence=await BB_DAILY_VAD_CONFIDENCE(),
        start_secs=await BB_DAILY_VAD_START_SECS(),
        stop_secs=await BB_DAILY_VAD_STOP_SECS(),
        min_volume=await BB_DAILY_VAD_MIN_VOLUME(),
    )


async def create_telephony_vad_params() -> VADParams:
    """Create VAD parameters for telephony mode from Redis dynamic config."""
    return VADParams(
        confidence=await BB_TELEPHONY_VAD_CONFIDENCE(),
        start_secs=await BB_TELEPHONY_VAD_START_SECS(),
        stop_secs=await BB_TELEPHONY_VAD_STOP_SECS(),
        min_volume=await BB_TELEPHONY_VAD_MIN_VOLUME(),
    )


def _layer_template_vad(
    template: Optional[TemplateModel], defaults: VADParams
) -> VADParams:
    """Layer template.configurations.vad_config per-field over defaults.

    Shared by Daily and telephony builders — the only thing that differs
    between modes is the source of the defaults (Redis key set).
    """
    template_vad = (
        template.configurations.vad_config
        if template and template.configurations
        else None
    )
    if not template_vad:
        return defaults

    logger.info(f"Template VAD config: {template_vad}")
    return VADParams(
        confidence=(
            template_vad.confidence
            if template_vad.confidence is not None
            else defaults.confidence
        ),
        start_secs=(
            template_vad.start_secs
            if template_vad.start_secs is not None
            else defaults.start_secs
        ),
        stop_secs=(
            template_vad.stop_secs
            if template_vad.stop_secs is not None
            else defaults.stop_secs
        ),
        min_volume=(
            template_vad.min_volume
            if template_vad.min_volume is not None
            else defaults.min_volume
        ),
    )


async def build_daily_vad_params(template: Optional[TemplateModel]) -> VADParams:
    """Build VAD params for Daily mode: template > Redis `BB_DAILY_VAD_*`."""
    return _layer_template_vad(template, await create_daily_vad_params())


async def build_telephony_vad_params(template: Optional[TemplateModel]) -> VADParams:
    """Build VAD params for telephony mode: template > Redis `BB_TELEPHONY_VAD_*`."""
    return _layer_template_vad(template, await create_telephony_vad_params())


async def create_vad_analyzer(
    is_daily_mode: bool,
    template: Optional[TemplateModel] = None,
) -> tuple[Optional[SileroVADAnalyzer], Optional[VADParams]]:
    """Create VAD analyzer with appropriate parameters.

    VAD is gated behind BREEZE_BUDDY_ENABLE_VAD (default False).
    When disabled, returns (None, None) and all VAD-related functionality is skipped.

    Both Daily and telephony modes honor template-level `vad_config` with per-field
    fallback to mode-specific Redis defaults (`BB_DAILY_VAD_*` / `BB_TELEPHONY_VAD_*`).

    Args:
        is_daily_mode: Whether this is Daily mode
        template: Template model used for VAD param overrides

    Returns:
        Tuple of (SileroVADAnalyzer or None, default_vad_params or None).
        The default_vad_params is returned so node-level VAD overrides can reset
        back to the call-level default (see `reset_vad_to_default` below).
    """
    if not await BREEZE_BUDDY_ENABLE_VAD():
        logger.info("VAD disabled (BREEZE_BUDDY_ENABLE_VAD=false)")
        return None, None

    if is_daily_mode:
        params = await build_daily_vad_params(template)
        sample_rate = DAILY_SAMPLE_RATE
    else:
        params = await build_telephony_vad_params(template)
        sample_rate = TELEPHONY_SAMPLE_RATE

    return SileroVADAnalyzer(sample_rate=sample_rate, params=params), params


# --- Runtime VAD mutation (called during a live call) ---


def reset_vad_to_default(context: TemplateContext):
    """Reset VAD params to the call-level default captured at startup."""
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

    if not hasattr(bot, "flow_config") or not bot.flow_config:
        return

    nodes = bot.flow_config.get("nodes", {})
    if node_name not in nodes:
        return

    node_config = nodes[node_name]
    # NodeConfig is a TypedDict, so access vad_config as a dict key
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
    """Mute STT by setting VAD confidence to 1.0 (impossible to trigger).

    Stores previous params so they can be restored on unmute.
    Only call this when context.vad_analyzer is available.
    """
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


def unmute_vad(context: TemplateContext):
    """Restore VAD params after a mute.

    Tries stored pre-mute params first, then the call-level default captured
    at agent startup (template layered over Redis). Only call this when
    context.vad_analyzer is available — which also guarantees
    bot.default_vad_params is set (see create_vad_analyzer).
    """
    old_confidence = context.vad_analyzer.params.confidence
    bot = context.bot

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
        bot._pre_mute_vad_params = None
        return

    if not (hasattr(bot, "default_vad_params") and bot.default_vad_params):
        logger.warning(
            f"STT unmute_vad called with no stored or default params for "
            f"call {context.call_sid}; leaving VAD params unchanged"
        )
        return

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
