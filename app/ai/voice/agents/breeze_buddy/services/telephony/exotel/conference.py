from typing import Callable, Dict, Optional

from app.core.logger import logger


class ExotelConferenceService:
    """Manage Exotel transfers via Calls/connect API (auto-bridged with CallType=trans)."""

    def __init__(
        self,
        account_sid: str,
        api_key: str,
        api_token: str,
        subdomain: str,
    ):
        self.account_sid = account_sid
        self.api_key = api_key
        self.api_token = api_token
        self.subdomain = subdomain
        self.base_url = (
            f"https://{api_key}:{api_token}@{subdomain}/v1/Accounts/{account_sid}"
        )

    async def handle_transfer(
        self,
        conference_name: str,
        agent_phone_number: str,
        customer_call_sid: str,
        telephony_number: str,
        callback: Optional[Callable] = None,
        status_callback_url: Optional[str] = None,
        customer_phone_number: Optional[str] = None,
    ) -> Dict:
        """
        Event-driven Exotel transfer.

        Returns immediately with success. Actual transfer is handled via:
        1. Redis flag is set by transfer handler before calling this
        2. Exotel calls /callback/transfer/dial-up to get agent number
        3. Callback endpoint checks Redis and returns agent number or denies

        Args:
            conference_name: Conference identifier
            agent_phone_number: Not used (agent lookup happens in callback)
            customer_call_sid: Customer call SID
            telephony_number: Not used
            callback: Not used
            status_callback_url: Not used
            customer_phone_number: Not used

        Returns:
            Success dict immediately
        """
        logger.info(
            f"[EXOTEL TRANSFER] Returning success immediately for call {customer_call_sid}. "
            f"Waiting for Exotel to callback dial-up endpoint."
        )

        return {
            "success": True,
            "conference_id": customer_call_sid,  # Use call_sid as conference identifier
            "agent_call_id": customer_call_sid,  # Will be updated when agent answers
        }
