from pipecat.processors.frameworks.rtvi import RTVIProcessor, RTVIServerMessageFrame

from app.agents.voice.automatic.rtvi.events_store import get_pending_rtvi_events
from app.core.logger import logger


async def emit_rtvi_event(rtvi: RTVIProcessor, event, session_id) -> None:
    """Emit conversation event via RTVI."""
    try:
        await rtvi.push_frame(
            RTVIServerMessageFrame(data={"type": event.type, "payload": event.payload})
        )
    except Exception as e:
        logger.error(f"Error emitting RTVI event for session {session_id}: {e}")


async def emit_pending_rtvi_events(
    rtvi: RTVIProcessor, function_name: str, session_id: str
) -> None:
    """Emit pending RTVI events after function calls."""
    del function_name  # Unused parameter
    try:
        pending_events = get_pending_rtvi_events(session_id)

        for event_data in pending_events:
            await rtvi.push_frame(
                RTVIServerMessageFrame(
                    data={"type": event_data["type"], "payload": event_data["payload"]}
                )
            )
            logger.info(
                f"Emitted RTVI event '{event_data['type']}' for session {session_id}"
            )

    except Exception as e:
        logger.error(
            f"Error emitting pending RTVI events for session {session_id}: {e}"
        )
