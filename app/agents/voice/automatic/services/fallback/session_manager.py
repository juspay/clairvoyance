"""
Fallback session manager for handling automatic session restart during STT fallback scenarios.
"""

import asyncio
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

from app.core.logger import logger


@dataclass
class FallbackSessionContext:
    """Context information for fallback sessions."""

    original_session_id: str
    room_url: str
    token: str
    bot_name: str
    original_stt_provider: str
    fallback_stt_provider: str
    error_reason: str
    session_args: Dict[str, Any]
    should_auto_restart: bool = True


class FallbackSessionManager:
    """Manages fallback session state and auto-restart logic."""

    def __init__(self):
        # Store fallback contexts for sessions that need auto-restart
        self._fallback_contexts: Dict[str, FallbackSessionContext] = {}
        self._restart_delay = 1.0  # seconds to wait before restarting

    def register_fallback_session(self, context: FallbackSessionContext):
        """Register a session for fallback auto-restart.

        Args:
            context: Fallback session context with restart parameters
        """
        self._fallback_contexts[context.original_session_id] = context
        logger.info(
            f"Registered fallback session {context.original_session_id} for auto-restart"
        )
        logger.debug(
            f"Fallback contexts now contains: {list(self._fallback_contexts.keys())}"
        )

    def get_fallback_context(self, session_id: str) -> Optional[FallbackSessionContext]:
        """Get fallback context for a session.

        Args:
            session_id: The session ID to get context for

        Returns:
            FallbackSessionContext if session is registered for fallback, None otherwise
        """
        logger.debug(f"Looking up fallback context for session {session_id}")
        logger.debug(
            f"Available fallback contexts: {list(self._fallback_contexts.keys())}"
        )
        context = self._fallback_contexts.get(session_id)
        logger.debug(f"Found context: {context is not None}")
        return context

    def remove_fallback_session(self, session_id: str):
        """Remove a session from fallback tracking.

        Args:
            session_id: The session ID to remove
        """
        if session_id in self._fallback_contexts:
            del self._fallback_contexts[session_id]
            logger.debug(f"Removed fallback session {session_id} from tracking")

    def is_fallback_session(self, session_id: str) -> bool:
        """Check if a session is registered for fallback auto-restart.

        Args:
            session_id: The session ID to check

        Returns:
            True if session should auto-restart, False otherwise
        """
        return session_id in self._fallback_contexts

    async def schedule_auto_restart(
        self, context: FallbackSessionContext, restart_callback
    ):
        """Schedule automatic restart of a fallback session.

        Args:
            context: Fallback session context
            restart_callback: Async function to call for restarting the session
        """
        logger.info(
            f"Scheduling auto-restart for session {context.original_session_id} in {self._restart_delay}s"
        )

        # Wait before restarting to allow cleanup
        await asyncio.sleep(self._restart_delay)

        try:
            # Call the restart callback with updated session parameters
            await restart_callback(context)
            logger.info(
                f"Auto-restart completed for session {context.original_session_id}"
            )
        except Exception as e:
            logger.error(
                f"Failed to auto-restart session {context.original_session_id}: {e}"
            )
        finally:
            # Clean up the fallback context
            self.remove_fallback_session(context.original_session_id)

    def create_restart_args(self, context: FallbackSessionContext) -> Dict[str, Any]:
        """Create new session arguments for restart with fallback STT provider.

        Args:
            context: Fallback session context

        Returns:
            Dictionary of session arguments for the restarted session
        """
        # Copy original session args and update STT provider context
        restart_args = context.session_args.copy()

        # Mark this as a fallback session restart
        restart_args["is_fallback_restart"] = True
        restart_args["original_stt_provider"] = context.original_stt_provider
        restart_args["fallback_stt_provider"] = context.fallback_stt_provider
        restart_args["fallback_reason"] = context.error_reason

        return restart_args

    def get_session_stats(self) -> Dict[str, Any]:
        """Get statistics about fallback sessions.

        Returns:
            Dictionary with fallback session statistics
        """
        return {
            "active_fallback_sessions": len(self._fallback_contexts),
            "session_ids": list(self._fallback_contexts.keys()),
            "restart_delay": self._restart_delay,
        }


# Global fallback session manager instance
_fallback_manager: Optional[FallbackSessionManager] = None


def get_fallback_session_manager() -> FallbackSessionManager:
    """Get the global fallback session manager instance.

    Returns:
        FallbackSessionManager singleton instance
    """
    global _fallback_manager
    if _fallback_manager is None:
        logger.debug("Creating new FallbackSessionManager instance")
        _fallback_manager = FallbackSessionManager()
    else:
        logger.debug(
            f"Returning existing FallbackSessionManager instance with {len(_fallback_manager._fallback_contexts)} contexts"
        )
    return _fallback_manager
