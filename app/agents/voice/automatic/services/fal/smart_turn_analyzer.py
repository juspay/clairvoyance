"""
Fal.ai Smart Turn service for managing smart turn analyzer lifecycle and session management.
"""

from typing import Optional, Tuple

from pipecat.audio.turn.smart_turn.fal_smart_turn import FalSmartTurnAnalyzer

from app.core import config
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session


class FalSmartTurnService:
    """Service for managing Fal.ai Smart Turn analyzer with proper session lifecycle."""

    def __init__(self):
        self._session = None
        self._analyzer = None

    async def create_analyzer(
        self,
    ) -> Tuple[Optional[FalSmartTurnAnalyzer], Optional[object]]:
        """
        Create and initialize Fal.ai Smart Turn analyzer with session management.

        Returns:
            Tuple of (analyzer, session) - both None if initialization fails
        """
        try:
            self._session = create_aiohttp_session()
            self._analyzer = FalSmartTurnAnalyzer(
                aiohttp_session=self._session, api_key=config.FAL_SMART_TURN_API_KEY
            )
            logger.info(
                "SMART_TURN: Fal.ai Smart Turn analyzer configured for transport-level integration"
            )
            return self._analyzer, self._session

        except Exception:
            logger.exception("SMART_TURN: Fal.ai Smart Turn initialization failed")
            await self._cleanup_session()
            return None, None

    async def cleanup(self, session=None) -> None:
        """
        Clean up the aiohttp session and resources.

        Args:
            session: Optional session to clean up (uses internal session if None)
        """
        target_session = session or self._session
        if target_session and not target_session.closed:
            await target_session.close()
            logger.debug("SMART_TURN: Fal.ai Smart Turn session closed")

        # Reset internal state
        self._session = None
        self._analyzer = None

    async def _cleanup_session(self) -> None:
        """Internal helper to clean up session on failure."""
        if self._session:
            await self._session.close()
        self._session = None
        self._analyzer = None
