import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from fastapi import WebSocket

from app.schemas import CallProvider, TelephonyConfig


@dataclass(frozen=True, slots=True)
class OutboundCallContext:
    """Provider callback context for a single outbound lead placement."""

    reseller_id: Optional[str] = None
    template_id: Optional[str] = None
    lead_id: Optional[str] = None
    telephony_number_id: Optional[str] = None


class OutboundCallPlacementKind(str, Enum):
    """Provider-neutral result of submitting an outbound call."""

    STARTED = "started"
    SUBMITTED = "submitted"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OutboundCallPlacement:
    """Provider-neutral call placement result."""

    kind: OutboundCallPlacementKind
    sid: Optional[str] = None
    submission_id: Optional[str] = None
    api_id: Optional[str] = None
    error_type: Optional[str] = None
    message: Optional[str] = None
    retryable: bool = False

    @classmethod
    def started(
        cls,
        sid: str,
        *,
        submission_id: Optional[str] = None,
        api_id: Optional[str] = None,
    ) -> "OutboundCallPlacement":
        return cls(
            kind=OutboundCallPlacementKind.STARTED,
            sid=sid,
            submission_id=submission_id,
            api_id=api_id,
        )

    @classmethod
    def submitted(
        cls, submission_id: str, *, api_id: Optional[str] = None
    ) -> "OutboundCallPlacement":
        return cls(
            kind=OutboundCallPlacementKind.SUBMITTED,
            submission_id=submission_id,
            api_id=api_id,
        )

    @classmethod
    def rejected(
        cls,
        message: str,
        *,
        error_type: Optional[str] = None,
        api_id: Optional[str] = None,
        retryable: bool = False,
    ) -> "OutboundCallPlacement":
        return cls(
            kind=OutboundCallPlacementKind.REJECTED,
            error_type=error_type,
            message=message,
            api_id=api_id,
            retryable=retryable,
        )

    @classmethod
    def unknown(
        cls, message: str, *, error_type: Optional[str] = None
    ) -> "OutboundCallPlacement":
        return cls(
            kind=OutboundCallPlacementKind.UNKNOWN,
            error_type=error_type,
            message=message,
            retryable=False,
        )


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
        context: Optional[OutboundCallContext] = None,
    ) -> Optional[OutboundCallPlacement]:
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
            context: Optional callback context for providers that need it
        """

    async def make_call_async(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
        context: Optional[OutboundCallContext] = None,
    ) -> Optional[OutboundCallPlacement]:
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
            context,
        )

    def set_completion_callback(self, callback):
        """
        Set the callback function to be called when the call is completed.
        """
        self.completion_callback = callback
