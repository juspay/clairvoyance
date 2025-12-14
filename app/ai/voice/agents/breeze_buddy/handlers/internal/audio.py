from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.utils.common import (
    load_audio,
)
from app.core.logger import logger


async def play_audio_sound(context: TemplateContext, args, transition_to=None):
    """
    Play an audio sound file through the transport.

    This handler:
    1. Loads an audio file from the specified path
    2. Writes it to the transport output for playback

    Args:
        context: Handler context with bot state access
        args: Additional arguments - should contain 'audio_path' key
        transition_to: Target node to transition to (not used for action handlers)

    Returns:
        Empty dict
    """
    logger.debug(
        f"play_audio_sound called for call {context.call_sid} with args: {args}"
    )

    # TODO: Make audio_path configurable
    audio_path = "app/ai/voice/agents/breeze_buddy/static/audio/cough.wav"

    logger.info(
        f"Attempting to play audio from: {audio_path} for call {context.call_sid}"
    )

    audio = load_audio(audio_path=audio_path)
    if audio:
        try:
            await context.transport.output().write_audio_frame(audio)
            logger.info(
                f"Successfully played audio from {audio_path} for call {context.call_sid}"
            )
        except Exception as e:
            logger.error(
                f"Failed to play audio from {audio_path} for call {context.call_sid}: {e}",
                exc_info=True,
            )
    else:
        logger.warning(
            f"Audio not loaded from {audio_path} for call {context.call_sid}, skipping playback"
        )

    return {}
