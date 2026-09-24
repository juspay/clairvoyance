from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.vad import mute_vad, unmute_vad
from app.core.logger import logger


async def mute_stt(context: TemplateContext, args, transition_to=None):
    """
    Mute STT input.

    Engages both mechanisms when available:
    - VAD enabled: also mutes VAD
    - TranscriptionGateProcessor: hard-drops all transcripts at the source

    VAD mute alone doesn't stop a turn from starting — Soniox keeps
    transcribing regardless, and TranscriptionUserTurnStartStrategy reacts
    to those transcripts independently of VAD state. The gate is what
    actually stops a transcript from reaching the aggregator.

    Accepts an optional ``duration`` (seconds) in *args*. When provided, the
    mute is automatically released after that many seconds. Without it the
    mute is indefinite until an explicit ``unmute_stt`` call.

    Example JSON action with duration::

        {
            "type": "function",
            "handler": "mute_stt",
            "args": {"duration": 5}
        }
    """
    duration = args.get("args", {}).get("duration") if args else None
    logger.debug(
        f"mute_stt called for call {context.call_sid} " f"(duration={duration})"
    )

    if context.vad_analyzer:
        mute_vad(context, float(duration) if duration is not None else None)

    if context.speech_gate:
        if duration is not None:
            context.speech_gate.mute_for(float(duration))
        else:
            context.speech_gate.mute()
        logger.info(
            f"STT muted via TranscriptionGate for call {context.call_sid} "
            f"(duration={duration})"
        )

    if not context.vad_analyzer and not context.speech_gate:
        logger.warning(
            f"No VAD analyzer or speech gate found for call {context.call_sid}, cannot mute STT"
        )


async def unmute_stt(context: TemplateContext, args, transition_to=None):
    """
    Unmute STT input.

    Releases both mechanisms when available, mirroring mute_stt:
    - VAD enabled: unmute_vad (restores stored/default params)
    - TranscriptionGateProcessor: releases hard mute

    Also cancels any pending timed-unmute task on either side, so an
    explicit unmute always takes precedence over a scheduled one.
    """
    logger.debug(f"unmute_stt called for call {context.call_sid}")

    if context.vad_analyzer:
        unmute_vad(context)

    if context.speech_gate:
        context.speech_gate.unmute()
        logger.info(f"STT unmuted via TranscriptionGate for call {context.call_sid}")

    if not context.vad_analyzer and not context.speech_gate:
        logger.warning(
            f"No VAD analyzer or speech gate found for call {context.call_sid}, cannot unmute STT"
        )
