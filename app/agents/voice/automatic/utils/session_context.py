"""
Session context for voice agent.
Provides session information through explicit context passing and global session ID management.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from app.core.logger import logger


@dataclass
class SessionContext:
    """Context object containing session information."""

    session_id: str

    def __post_init__(self):
        logger.info(f"Created session context with ID: {self.session_id}")


def create_session_context(session_id: str) -> SessionContext:
    """Create a new session context."""
    return SessionContext(session_id=session_id)


# Global session ID storage
_current_session_id: Optional[str] = None

# Global image history storage per session
_session_image_history: Dict[str, List[Any]] = {}


@dataclass
class ImageHistoryItem:
    """Single item in image history."""

    file_path: str
    url: str
    prompt: str
    workflow_type: str
    timestamp: datetime
    metadata: Dict[str, Any] = field(default_factory=dict)


def set_current_session_id(session_id: str) -> None:
    """Set the current session ID for global access."""
    global _current_session_id
    _current_session_id = session_id
    logger.debug(f"Set global session ID: {session_id}")

    # Initialize image history for this session if not exists
    if session_id not in _session_image_history:
        _session_image_history[session_id] = []
        logger.debug(f"Initialized image history for session: {session_id}")


def get_current_session_id() -> Optional[str]:
    """Get the current session ID for global access."""
    global _current_session_id
    return _current_session_id


def add_image_to_history(
    session_id: str,
    file_path: str,
    url: str,
    prompt: str,
    workflow_type: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Add an image to the session's history."""
    global _session_image_history

    if session_id not in _session_image_history:
        _session_image_history[session_id] = []

    image_item = ImageHistoryItem(
        file_path=file_path,
        url=url,
        prompt=prompt,
        workflow_type=workflow_type,
        timestamp=datetime.now(),
        metadata=metadata or {},
    )

    _session_image_history[session_id].append(image_item)
    logger.info(f"Added image to history for session {session_id}: {file_path}")


def get_session_image_history(session_id: str) -> List[ImageHistoryItem]:
    """Get all images in the session's history."""
    global _session_image_history
    return _session_image_history.get(session_id, [])


def get_previous_image(session_id: str) -> Optional[ImageHistoryItem]:
    """Get the most recent image from the session's history."""
    history = get_session_image_history(session_id)
    return history[-1] if history else None


def get_nth_previous_image(session_id: str, n: int = 1) -> Optional[ImageHistoryItem]:
    """Get the nth previous image (1 = last, 2 = second to last, etc.)."""
    history = get_session_image_history(session_id)
    if len(history) >= n:
        return history[-n]
    return None


def clear_session_image_history(session_id: str) -> None:
    """Clear all images from the session's history."""
    global _session_image_history
    if session_id in _session_image_history:
        del _session_image_history[session_id]
        logger.info(f"Cleared image history for session: {session_id}")


def find_images_by_type(session_id: str, workflow_type: str) -> List[ImageHistoryItem]:
    """Find all images of a specific workflow type in the session."""
    history = get_session_image_history(session_id)
    return [item for item in history if item.workflow_type == workflow_type]
