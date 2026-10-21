"""Template-level switches for meeting recording, read by the agent.

Kept apart from the session so the agent imports one tiny module: is it on,
what to force on the STT config, and how to let go of a live session when the
connection ends.
"""

from typing import Any, Optional

from app.core.logger import logger


def meeting_recording_enabled(configurations: Any) -> bool:
    """True only when the template carries ``meeting_recording.enabled``."""
    config = getattr(configurations, "meeting_recording", None)
    return bool(config is not None and config.enabled)


def prepare_configurations_for_meeting_recording(configurations: Any) -> bool:
    """Force speaker diarization on for a recording template (Soniox).

    Mutates the loaded configuration object in place, before the STT
    service is built, so "who spoke" lands on every token. Returns True when
    something was changed. A template without the block is untouched.
    """
    if not meeting_recording_enabled(configurations):
        return False
    stt = getattr(configurations, "stt_configuration", None)
    soniox = getattr(stt, "soniox", None) if stt is not None else None
    if soniox is None:
        return False
    if getattr(soniox, "enable_speaker_diarization", None) is True:
        return False
    soniox.enable_speaker_diarization = True
    logger.info("Meeting recording: speaker diarization forced on for Soniox")
    return True


async def suspend_meeting_recording(bot_instance: Any) -> Optional[int]:
    """Let a live session go when the call ends, without finishing it.

    Returns lines flushed, or None when there was nothing to suspend.
    Never raises: this runs on the teardown path.
    """
    session = getattr(bot_instance, "_meeting_session", None)
    if session is None or not getattr(session, "is_recording", False):
        return None
    try:
        return await session.suspend()
    except Exception as e:
        logger.error(f"[MEETING] suspend on teardown failed: {e}")
        return None
