from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.vad import mute_vad, unmute_vad
from app.core.logger import logger


async def mute_stt(context: TemplateContext, args, transition_to=None):
    """
    Mute STT input.

    Routes to the appropriate mute mechanism:
    - VAD enabled: delegates to mute_vad (duration arg is ignored)
    - VAD disabled: engages TranscriptionGateProcessor hard mute

    Accepts an optional ``duration`` (seconds) in *args*. When provided and
    the TranscriptionGate path is active, the mute is automatically released
    after that many seconds. Without it the mute is indefinite until an
    explicit ``unmute_stt`` call. The duration arg is ignored on the VAD path.

    Example JSON action with duration::

        {
            "type": "function",
            "handler": "mute_stt",
            "args": {"duration": 5}
        }
    """
    duration = args.get("duration") if args else None
    logger.debug(
        f"mute_stt called for call {context.call_sid} " f"(duration={duration})"
    )

    if context.vad_analyzer:
        if duration is not None:
            logger.debug(
                f"duration arg ignored for VAD path on call {context.call_sid}"
            )
        mute_vad(context)
    elif context.speech_gate:
        if duration is not None:
            context.speech_gate.mute_for(float(duration))
        else:
            context.speech_gate.mute()
        logger.info(
            f"STT muted via TranscriptionGate for call {context.call_sid} "
            f"(VAD disabled, duration={duration})"
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

    Also cancels any pending timed-unmute task so that an explicit unmute
    takes precedence over a previously scheduled auto-unmute.
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
