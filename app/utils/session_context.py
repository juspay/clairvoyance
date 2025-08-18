"""
Global session context for voice agent.
Allows chart tools and other functions to access session information.
"""

from typing import Optional
from app.core.logger import logger

# Global session context storage
_current_session_id: Optional[str] = None


def set_current_session_id(session_id: str):
    """Set the current session ID globally."""
    global _current_session_id
    _current_session_id = session_id
    logger.info(f"Set global session ID: {session_id}")


def get_current_session_id() -> Optional[str]:
    """Get the current session ID."""
    return _current_session_id


def clear_current_session_id():
    """Clear the current session ID."""
    global _current_session_id
    _current_session_id = None
    logger.info("Cleared global session ID")