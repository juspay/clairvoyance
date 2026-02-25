from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.vad import mute_vad, unmute_vad
from app.core.logger import logger


async def mute_stt(context: TemplateContext, args, transition_to=None):
    """
    Mute STT input.

    Routes to the appropriate mute mechanism:
    - VAD enabled: delegates to mute_vad (sets confidence=1.0)
    - VAD disabled: engages TranscriptionGateProcessor hard mute
    """
    logger.debug(f"mute_stt called for call {context.call_sid}")
    if context.vad_analyzer:
        mute_vad(context)
    elif context.speech_gate:
        context.speech_gate.mute()
        logger.info(
            f"STT muted via TranscriptionGate for call {context.call_sid} (VAD disabled)"
        )
    else:
        logger.warning(
            f"No VAD analyzer or speech gate found for call {context.call_sid}, cannot mute STT"
        )


async def unmute_stt(context: TemplateContext, args, transition_to=None):
    """
    Unmute STT input.

    Routes to the appropriate unmute mechanism:
    - VAD enabled: delegates to unmute_vad (restores stored/default params)
    - VAD disabled: releases TranscriptionGateProcessor hard mute
    """
    logger.debug(f"unmute_stt called for call {context.call_sid}")
    if context.vad_analyzer:
        unmute_vad(context)
    elif context.speech_gate:
        context.speech_gate.unmute()
        logger.info(
            f"STT unmuted via TranscriptionGate for call {context.call_sid} (VAD disabled)"
        )
    else:
        logger.warning(
            f"No VAD analyzer or speech gate found for call {context.call_sid}, cannot unmute STT"
        )
