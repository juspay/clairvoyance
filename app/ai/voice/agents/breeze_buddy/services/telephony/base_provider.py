import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from fastapi import WebSocket

from app.schemas import CallProvider, TelephonyConfig


class VoiceCallProvider(ABC):
    """
    Abstract base class for voice call providers.
    """

    def __init__(
        self,
        config,
        aiohttp_session,
        telephony_config: Optional[TelephonyConfig] = None,
    ):
        self.config = config
        self.aiohttp_session = aiohttp_session
        self.telephony_config = telephony_config
        self.completion_callback = None
        self.conference_service: Any = None

    async def use_template_credentials(
        self, accounts: Any, configurations: Any
    ) -> bool:
        """Switch this call's REST client to the account the template's
        ``telephony_configuration`` names, once per call; True when it chose
        now. Raises AccountRefused when that row may not serve. Default: the
        provider has one account, the environment's."""
        return False

    def set_hangup_credentials(self, transport: Any) -> None:
        """Hand the call's account to the transport's serializer (its
        auto hang-up). Default: the serializer keeps the environment's keys."""

    @abstractmethod
    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        """
        Handle the WebSocket connection for the voice provider.
        """

    @abstractmethod
    def make_call(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Initiate a call.

        This is intentionally synchronous — all provider SDKs (Twilio, Plivo,
        Exotel) use blocking HTTP clients. NEVER call this from an ``async def``:
        use ``make_call_async`` instead, which offloads it to a worker thread.

        Args:
            customer_mobile_number: Phone number to call
            telephony_number: Caller ID / telephony number
            reseller_id: Optional merchant ID for tiered pod allocation
            template_name: Optional template name for WebSocket path routing
        """

    async def make_call_async(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Await-able wrapper around ``make_call`` that keeps it off the event loop.

        Every provider SDK below this line is synchronous — Plivo and Twilio
        ship blocking REST clients, Exotel uses ``requests``. Calling
        ``make_call`` directly from an ``async def`` freezes the single
        uvicorn worker for the whole provider round-trip (~150-500ms), which
        with ~20 concurrent dispatch workers starves every inbound answer
        sharing the loop.

        All callers in async context MUST use this instead of ``make_call``.
        """
        return await asyncio.to_thread(
            self.make_call,
            customer_mobile_number,
            telephony_number,
            reseller_id,
            template_name,
        )

    def set_completion_callback(self, callback):
        """
        Set the callback function to be called when the call is completed.
        """
        self.completion_callback = callback
