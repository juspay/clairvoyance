"""
Session-based store for pending RTVI events.
Similar to chart emissions but for direct RTVI events.
"""

from typing import Any, Dict, List

from app.core.logger import logger

# Global store for pending RTVI events by session
_pending_rtvi_events: Dict[str, List[Dict[str, Any]]] = {}


def register_pending_rtvi_event(
    session_id: str, event_type: str, event_payload: Dict[str, Any]
) -> None:
    """Register an RTVI event to be emitted later."""
    if session_id not in _pending_rtvi_events:
        _pending_rtvi_events[session_id] = []

    event_data = {"type": event_type, "payload": event_payload}

    _pending_rtvi_events[session_id].append(event_data)
    logger.info(
        f"Registered pending RTVI event '{event_type}' for session {session_id}"
    )


def get_pending_rtvi_events(session_id: str) -> List[Dict[str, Any]]:
    """Get and clear pending RTVI events for a session."""
    events = _pending_rtvi_events.get(session_id, [])

    # Clear the events after retrieving them
    if session_id in _pending_rtvi_events:
        del _pending_rtvi_events[session_id]

    logger.debug(
        f"Retrieved {len(events)} pending RTVI events for session {session_id}"
    )
    return events


def clear_pending_rtvi_events(session_id: str) -> None:
    """Clear all pending RTVI events for a session."""
    if session_id in _pending_rtvi_events:
        del _pending_rtvi_events[session_id]
        logger.debug(f"Cleared pending RTVI events for session {session_id}")
