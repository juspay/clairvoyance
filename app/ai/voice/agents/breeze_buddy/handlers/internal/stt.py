from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.config.static import BREEZE_BUDDY_VAD_CONFIDENCE
from app.core.logger import logger


async def mute_stt(context: TemplateContext, args, transition_to=None):
    """
    Mute STT by setting VAD confidence to 1.0 .

    Args:
        context: Handler context with bot state access
        args: Additional arguments (not used)
        transition_to: Target node to transition to (not used for action handlers)
    """
    logger.debug(f"mute_stt called for call {context.call_sid}")

    if context.vad_analyzer:
        old_confidence = context.vad_analyzer.params.confidence
        context.vad_analyzer.params.confidence = 1.0
        logger.info(
            f"STT muted via VAD for call {context.call_sid} "
            f"(confidence: {old_confidence} -> 1.0)"
        )
    else:
        logger.warning(
            f"No VAD analyzer found for call {context.call_sid}, cannot mute STT"
        )


async def unmute_stt(context: TemplateContext, args, transition_to=None):
    """
    Unmute STT by resetting VAD confidence to its original value.

    Args:
        context: Handler context with bot state access
        args: Additional arguments (not used)
        transition_to: Target node to transition to (not used for action handlers)
    """
    logger.debug(f"unmute_stt called for call {context.call_sid}")

    if context.vad_analyzer:
        old_confidence = context.vad_analyzer.params.confidence
        context.vad_analyzer.params.confidence = BREEZE_BUDDY_VAD_CONFIDENCE
        logger.info(
            f"STT unmuted via VAD for call {context.call_sid} "
            f"(confidence: {old_confidence} -> {BREEZE_BUDDY_VAD_CONFIDENCE})"
        )
    else:
        logger.warning(
            f"No VAD analyzer found for call {context.call_sid}, cannot unmute STT"
        )
