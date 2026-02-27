"""
Plivo Transfer Service

Implements transfers by using Plivo's native Call Transfer API.
When a transfer is requested, the customer's live call leg is transferred
to a dial-up XML endpoint. This endpoint returns `<Dial><Number>` XML
which connects the customer directly to the agent.

Flow:
  1. ``calls.transfer(call_uuid=customer_call_sid, legs='aleg', aleg_url=<dial-up>)``
     → Plivo fetches the answer_url which returns ``<Dial><Number>{agent}</Number></Dial>``
       XML, making Plivo call the agent from the customer's leg.
  2. The agent phone number is resolved in the webhook callback.

Reference:
  - Dial XML:  https://www.plivo.com/docs/voice/xml/dial
  - Calls API: https://www.plivo.com/docs/voice/api/call#transfer-a-call
"""

from typing import Callable, Dict, Optional
from urllib.parse import urlencode

import plivo

from app.core.config.static import APP_BASE_URL
from app.core.logger import logger

# Callback path that returns <Dial><Number> XML to connect customer → agent
_DIAL_UP_PATH = "/agent/voice/breeze-buddy/plivo/callback/transfer/dial-up"


def _dial_up_url(
    customer_call_sid: str,
    outbound_number: str,
    status_callback_url: Optional[str] = None,
) -> str:
    """Build the URL for the dial-up callback during a call transfer."""
    params: Dict[str, str] = {
        "customer_call_sid": customer_call_sid,
        "outbound_number": outbound_number,
    }
    if status_callback_url:
        params["status_callback_url"] = status_callback_url
    return f"{APP_BASE_URL}{_DIAL_UP_PATH}?{urlencode(params)}"


class PlivoConferenceService:
    """
    Service for managing Plivo transfers.

    Despite the class name (kept for backward-compatibility with
    ``PlivoProvider`` which references ``self.conference_service``),
    this implementation does **not** use Plivo Conferences.

    Instead it transfers the live customer call using the native
    Plivo API to an endpoint that returns ``<Dial><Number>`` XML
    to bridge the agent to the customer's ongoing call.
    """

    def __init__(self, plivo_client: plivo.RestClient):
        self.client = plivo_client

    # ------------------------------------------------------------------
    # Transfer customer call to `<Dial><Number>` endpoint
    # ------------------------------------------------------------------

    def _transfer_call(
        self,
        customer_call_sid: str,
        outbound_number: str,
        status_callback_url: Optional[str] = None,
    ) -> Dict:
        """
        Transfer the customer's live call via the Plivo native Transfer API.

        The ``aleg_url`` points to a callback that returns ``<Dial><Number>``
        XML to dial the agent.
        """
        answer_url = _dial_up_url(
            customer_call_sid=customer_call_sid,
            outbound_number=outbound_number,
            status_callback_url=status_callback_url,
        )

        logger.info(
            f"[Transfer] Transferring customer call {customer_call_sid} — "
            f"bleg_url={answer_url}"
        )

        try:
            transfer_kwargs: Dict = dict(
                call_uuid=customer_call_sid,
                legs="aleg",
                aleg_url=answer_url,
                aleg_method="GET",
            )

            response = self.client.calls.transfer(**transfer_kwargs)

            logger.info(
                f"[Transfer] calls.transfer response: {response}, "
                f"type={type(response)}"
            )
            if hasattr(response, "__dict__"):
                logger.info(f"[Transfer] response.__dict__: {response.__dict__}")

            return {
                "success": True,
                "agent_call_uuid": customer_call_sid,
                "reason": "success",
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"[Transfer] Failed to transfer call "
                f"{customer_call_sid}: {error_message}"
            )

            if (
                "invalid" in error_message.lower()
                or "not a valid" in error_message.lower()
            ):
                reason = "invalid_call_uuid"
            elif "not found" in error_message.lower():
                reason = "call_not_found"
            else:
                reason = "transfer_api_error"

            return {
                "success": False,
                "reason": reason,
                "error": error_message,
            }

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def handle_transfer(
        self,
        conference_name: str,
        agent_phone_number: str,
        customer_call_sid: str,
        outbound_number: str,
        callback: Optional[Callable] = None,
        status_callback_url: Optional[str] = None,
        customer_phone_number: Optional[str] = None,
    ) -> Dict:
        """
        Execute transfer by using the Plivo native Transfer API.

        This delegates to `client.calls.transfer()`, transferring the
        customer's live leg to the callback endpoint which will return XML
        to connect the agent to the call.

        Returns dict matching the base-provider contract::

            {
                "success": bool,
                "conference_id": str,   # conference name (for compat)
                "agent_call_id": str,   # agent leg call UUID
                "reason": str,
            }
        """
        logger.info(
            f"[Transfer] Initiating transfer "
            f"'{conference_name}' — agent={agent_phone_number}, "
            f"customer_call_sid={customer_call_sid}"
        )

        try:
            # Transfer customer leg
            transfer_result = self._transfer_call(
                customer_call_sid=customer_call_sid,
                outbound_number=outbound_number,
                status_callback_url=status_callback_url,
            )

            if not transfer_result["success"]:
                logger.error(
                    f"[Transfer] Failed to transfer customer call: "
                    f"{transfer_result['reason']}"
                )
                return {
                    **transfer_result,
                    "conference_id": conference_name,
                    "agent_call_id": None,
                }

            agent_call_uuid = transfer_result["agent_call_uuid"]
            logger.info(
                f"[Transfer] Customer call {customer_call_sid} successfully transferred. "
                f"Agent will be dialled."
            )

            if callback:
                try:
                    callback(
                        conference_name,
                        agent_call_uuid,
                        customer_call_sid,
                    )
                except Exception as cb_err:
                    logger.warning(f"Callback execution failed: {cb_err}")

            return {
                "success": True,
                "conference_id": conference_name,
                "agent_call_id": agent_call_uuid,
                "reason": "success",
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"[Transfer] Unexpected error during transfer "
                f"for '{conference_name}': {error_message}",
                exc_info=True,
            )

            if "timeout" in error_message.lower():
                reason = "timeout"
            elif (
                "authentication" in error_message.lower()
                or "credentials" in error_message.lower()
            ):
                reason = "authentication_error"
            else:
                reason = "transfer_api_error"

            return {
                "success": False,
                "conference_id": conference_name,
                "agent_call_id": None,
                "reason": reason,
                "error": error_message,
            }
