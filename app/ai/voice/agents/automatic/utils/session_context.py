"""
Session context for voice agent.
Provides session information through explicit context passing and global session ID management.
"""

from dataclasses import dataclass
from typing import Optional

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


def set_current_session_id(session_id: str) -> None:
    """Set the current session ID for global access."""
    global _current_session_id
    _current_session_id = session_id
    logger.debug(f"Set global session ID: {session_id}")


def get_current_session_id() -> Optional[str]:
    """Get the current session ID for global access."""
    global _current_session_id
    return _current_session_id
