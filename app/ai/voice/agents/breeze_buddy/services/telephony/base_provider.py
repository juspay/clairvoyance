from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from fastapi import WebSocket

from app.schemas import CallProvider


class VoiceCallProvider(ABC):
    """
    Abstract base class for voice call providers.
    """

    def __init__(self, config, aiohttp_session):
        self.config = config
        self.aiohttp_session = aiohttp_session
        self.completion_callback = None

    @abstractmethod
    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        """
        Handle the WebSocket connection for the voice provider.
        """

    @abstractmethod
    def make_call(
        self,
        customer_mobile_number: str,
        outbound_number: str,
        merchant_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Initiate a call.

        This is intentionally synchronous — all provider SDKs (Twilio, Plivo,
        Exotel) use blocking HTTP clients. The caller should be aware this
        blocks the event loop briefly.

        Args:
            customer_mobile_number: Phone number to call
            outbound_number: Caller ID / outbound number
            merchant_id: Optional merchant ID for tiered pod allocation
            template_name: Optional template name for WebSocket path routing
        """

    def set_completion_callback(self, callback):
        """
        Set the callback function to be called when the call is completed.
        """
        self.completion_callback = callback
