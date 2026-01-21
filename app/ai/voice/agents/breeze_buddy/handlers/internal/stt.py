from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.vad import mute_vad, unmute_vad
from app.core.logger import logger


async def mute_stt(context: TemplateContext, args, transition_to=None):
    """
    Mute STT by setting VAD confidence to 1.0.

    Stores the previous VAD params so they can be restored when unmuting.

    Args:
        context: Handler context with bot state access
        args: Additional arguments (not used)
        transition_to: Target node to transition to (not used for action handlers)
    """
    logger.debug(f"mute_stt called for call {context.call_sid}")
    mute_vad(context)


async def unmute_stt(context: TemplateContext, args, transition_to=None):
    """
    Unmute STT by restoring the VAD params that were stored before muting.

    Falls back to template's default VAD params or static config if no stored params exist.

    Args:
        context: Handler context with bot state access
        args: Additional arguments (not used)
        transition_to: Target node to transition to (not used for action handlers)
    """
    logger.debug(f"unmute_stt called for call {context.call_sid}")
    unmute_vad(context)
