"""
Session-based storage for logo request context.
Stores original advertisement parameters when logo upload is requested.
Uses file-based storage for cross-process access between voice agent and web server.
"""

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.logger import logger

# File-based storage directory for cross-process access
LOGO_CONTEXT_DIR = Path("temp/logo_contexts")
LOGO_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)


def _get_context_file_path(session_id: str) -> Path:
    """Get the file path for a session's logo context."""
    return LOGO_CONTEXT_DIR / f"logo_context_{session_id}.json"


def store_logo_request_context(session_id: str, context: Dict[str, Any]) -> None:
    """Store the original advertisement request context for logo continuation."""
    try:
        context_file = _get_context_file_path(session_id)
        with open(context_file, "w") as f:
            json.dump(context, f, indent=2)
        logger.info(f"Stored logo request context for session {session_id}")
        logger.debug(f"Context: {context}")
    except Exception as e:
        logger.error(f"Failed to store logo context for session {session_id}: {e}")


def get_logo_request_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Get and clear the stored logo request context for a session."""
    try:
        context_file = _get_context_file_path(session_id)
        if not context_file.exists():
            logger.warning(f"No logo request context found for session {session_id}")
            return None

        with open(context_file, "r") as f:
            context = json.load(f)

        # Clear the context after retrieving it (one-time use)
        context_file.unlink()
        logger.info(
            f"Retrieved and cleared logo request context for session {session_id}"
        )
        logger.debug(f"Context: {context}")
        return context
    except Exception as e:
        logger.error(f"Failed to get logo context for session {session_id}: {e}")
        return None


def clear_logo_request_context(session_id: str) -> None:
    """Clear the logo request context for a session without retrieving it."""
    try:
        context_file = _get_context_file_path(session_id)
        if context_file.exists():
            context_file.unlink()
            logger.info(f"Cleared logo request context for session {session_id}")
    except Exception as e:
        logger.error(f"Failed to clear logo context for session {session_id}: {e}")


def has_logo_request_context(session_id: str) -> bool:
    """Check if there's a pending logo request context for a session."""
    context_file = _get_context_file_path(session_id)
    return context_file.exists()


def get_any_pending_logo_context() -> Optional[tuple[str, Dict[str, Any]]]:
    """Get any pending logo request context and its session ID."""
    try:
        for context_file in LOGO_CONTEXT_DIR.glob("logo_context_*.json"):
            session_id = context_file.stem.replace("logo_context_", "")
            with open(context_file, "r") as f:
                context = json.load(f)
            logger.info(f"Found pending logo context for session {session_id}")
            return session_id, context
    except Exception as e:
        logger.error(f"Failed to get any pending logo context: {e}")
    return None


def get_and_clear_any_pending_logo_context() -> Optional[tuple[str, Dict[str, Any]]]:
    """Get and clear any pending logo request context."""
    try:
        for context_file in LOGO_CONTEXT_DIR.glob("logo_context_*.json"):
            session_id = context_file.stem.replace("logo_context_", "")
            with open(context_file, "r") as f:
                context = json.load(f)
            # Clear the context after retrieving it
            context_file.unlink()
            logger.info(
                f"Retrieved and cleared pending logo context for session {session_id}"
            )
            return session_id, context
    except Exception as e:
        logger.error(f"Failed to get and clear any pending logo context: {e}")
    return None
