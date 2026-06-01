from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.core.logger import logger


async def emit_rtvi_event(rtvi: RTVIProcessor, event, session_id) -> None:
    """Emit conversation event via RTVI."""
    try:
        await rtvi.push_frame(
            RTVIServerMessageFrame(data={"type": event.type, "payload": event.payload})
        )
    except Exception as e:
        logger.error(f"Error emitting RTVI event for session {session_id}: {e}")
