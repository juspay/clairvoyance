import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import WebSocket

from app.schemas import CallProvider, TelephonyConfig

# ``make_call``'s ``status`` when the dial request was sent but no reply came
# back: the provider may have placed the call. Not a failure (that is None) —
# the dialler must neither redial nor release; the provider's own webhook
# settles it, matched to the lead through ``dial_ref``.
DIAL_OUTCOME_UNKNOWN = "unknown"

# ``make_call``'s ``status`` for Plivo's 429 when the caller asked with
# ``report_throttle=True`` (the v2 dialler): nothing was placed, and the same request
# may be sent again. Every other caller gets None for a 429, as before.
DIAL_OUTCOME_THROTTLED = "throttled"

# The lead's meta_data key that marks it held after such a dial, until a
# webhook claims it or the stuck sweep puts it back to be dialled again.
UNKNOWN_DIAL_META_KEY = "unknown_dial"


def dial_ref_time(at: datetime) -> str:
    """
    ``dial_at`` as it travels on a provider's webhook URLs: UTC ending in 'Z',
    so no '+'. A '+' survives one URL decode but turns into a space at any hop
    that decodes once too often, and the webhook's claim would then silently
    miss its dial. ``datetime.fromisoformat`` reads this back to the same
    instant, microseconds included, which the claim matches exactly.
    """
    return at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


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
        dial_ref: Optional[Dict[str, str]] = None,
        report_throttle: bool = False,
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
            dial_ref: Key/values the provider echoes back on this call's
                webhooks, so a dial whose reply was lost can still be matched
                to its lead. Plivo puts them on its answer + hangup URLs;
                providers that never return DIAL_OUTCOME_UNKNOWN ignore them.
            report_throttle: the v2 dialler sends the same request again after
                a 429, so it asks for DIAL_OUTCOME_THROTTLED instead of None.
                Plivo only; the other providers answer None, as always.
        """

    async def make_call_async(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
        dial_ref: Optional[Dict[str, str]] = None,
        report_throttle: bool = False,
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
            dial_ref=dial_ref,
            report_throttle=report_throttle,
        )

    async def is_call_live(self, lead: Any) -> Optional[bool]:
        """Ask the provider whether the lead's call is still in progress.

        True = live, False = ended or unknown to the provider, None = this
        provider cannot say (the default). Raises when the lookup fails; the
        caller treats that as "do not know", never as "ended".
        """
        return None

    def set_completion_callback(self, callback):
        """
        Set the callback function to be called when the call is completed.
        """
        self.completion_callback = callback
