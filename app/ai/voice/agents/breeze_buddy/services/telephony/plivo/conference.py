"""
Plivo Transfer Service

Implements warm transfers using Plivo's MultiPartyCall (MPC) API.

Flow (dial-first MPC):
  1. Agent is dialled into a new MPC via ``add_participant(role='agent')``.
     The customer's live ``<Stream>`` is untouched and Buddy stays connected.
  2. If the agent answers, a participant-state-changes webhook fires.
     The customer's existing call is then moved into the MPC via
     ``add_participant(call_uuid=...)``.  The ``<Stream>`` WebSocket dies
     at this point — expected, as Buddy is done.
  3. If the agent doesn't answer, the outbound leg simply times out.
     The customer's ``<Stream>`` was never modified, so Buddy continues
     the conversation seamlessly.

Reference:
  - Add Participant API: https://www.plivo.com/docs/voice/api/multiparty-calls#add-a-participant
"""

import asyncio
from functools import partial
from typing import Callable, Dict, Optional
from urllib.parse import urlencode

import plivo

from app.core.config.static import APP_BASE_URL
from app.core.logger import logger

# Callback path that returns <Dial><Number> XML to connect customer → agent
# during the legacy (non-MPC) immediate transfer.
_DIAL_UP_PATH = "/agent/voice/breeze-buddy/plivo/callback/transfer/dial-up"


def _dial_up_url(
    customer_call_sid: str,
    telephony_number: str,
    status_callback_url: Optional[str] = None,
) -> str:
    """Build the URL for the dial-up callback during a legacy call transfer."""
    params: Dict[str, str] = {
        "customer_call_sid": customer_call_sid,
        "outbound_number": telephony_number,
    }
    if status_callback_url:
        params["status_callback_url"] = status_callback_url
    return f"{APP_BASE_URL}{_DIAL_UP_PATH}?{urlencode(params)}"


class PlivoConferenceService:
    """
    Service for managing Plivo transfers using MultiPartyCall (MPC).

    The class name is retained for backward compatibility with
    ``PlivoProvider`` which references ``self.conference_service``.
    """

    def __init__(self, plivo_client: plivo.RestClient):
        self.client = plivo_client

    # ------------------------------------------------------------------
    # Add agent as outbound participant to an MPC
    # ------------------------------------------------------------------

    async def _add_agent_to_mpc(
        self,
        mpc_name: str,
        agent_phone_number: str,
        telephony_number: str,
        customer_call_sid: str,
    ) -> Dict:
        """
        Create a new MPC and add the agent as an outbound participant.

        The customer's call is **not** modified — it stays on ``<Stream>``.
        """
        status_callback_url = (
            f"{APP_BASE_URL}/agent/voice/breeze-buddy"
            f"/plivo/callback/transfer/mpc-transfer"
            f"?call_sid={customer_call_sid}"
        )

        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                partial(
                    self.client.multi_party_calls.add_participant,
                    friendly_name=mpc_name,
                    role="agent",
                    from_=telephony_number,
                    to_=agent_phone_number,
                    ring_timeout=30,
                    end_mpc_on_exit=True,
                    start_mpc_on_enter=True,
                    stay_alone=True,
                    status_callback_url=status_callback_url,
                    status_callback_events="participant-state-changes",
                ),
            )

            logger.info(f"[MPC] Agent added to MPC '{mpc_name}': response={response}")

            return {
                "success": True,
                "reason": "success",
                "agent_added": True,
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"[MPC] Failed to add agent to MPC '{mpc_name}': {error_message}"
            )
            return {
                "success": False,
                "reason": "mpc_add_participant_error",
                "error": error_message,
            }

    # ------------------------------------------------------------------
    # Move existing call into MPC
    # ------------------------------------------------------------------

    async def move_customer_to_mpc(
        self,
        call_sid: str,
        mpc_name: str,
    ) -> Dict:
        """
        Move the customer's existing call leg into the MPC.

        This terminates the ``<Stream>`` WebSocket — expected after the
        agent has answered and Buddy should disconnect.
        """
        try:
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                partial(
                    self.client.multi_party_calls.add_participant,
                    friendly_name=mpc_name,
                    call_uuid=call_sid,
                    role="customer",
                    end_mpc_on_exit=True,
                ),
            )

            logger.info(
                f"[MPC] Customer {call_sid} moved to MPC '{mpc_name}': "
                f"response={response}"
            )

            return {
                "success": True,
                "reason": "success",
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"[MPC] Failed to move customer {call_sid} to MPC "
                f"'{mpc_name}': {error_message}"
            )
            return {
                "success": False,
                "reason": "mpc_move_customer_error",
                "error": error_message,
            }

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    async def handle_mpc_transfer(
        self,
        conference_name: str,
        agent_phone_number: str,
        customer_call_sid: str,
        telephony_number: str,
    ) -> Dict:
        """
        Execute a warm transfer by dialling the agent into an MPC.

        The customer's ``<Stream>`` leg is left untouched.  The outcome
        (answered / unavailable) is delivered asynchronously via the MPC
        participant-state-changes webhook which publishes to a Redis
        pub/sub channel.

        Returns dict matching the base-provider contract::

            {
                "success": bool,
                "conference_id": str,
                "agent_call_id": str,
                "reason": str,
            }
        """
        mpc_name = f"transfer-{customer_call_sid}"

        logger.info(
            f"[Transfer] Initiating MPC transfer '{mpc_name}' — "
            f"agent={agent_phone_number}, customer={customer_call_sid}"
        )

        try:
            result = await self._add_agent_to_mpc(
                mpc_name=mpc_name,
                agent_phone_number=agent_phone_number,
                telephony_number=telephony_number,
                customer_call_sid=customer_call_sid,
            )

            if not result["success"]:
                return {
                    **result,
                    "conference_id": mpc_name,
                    "agent_call_id": None,
                }

            return {
                "success": True,
                "conference_id": mpc_name,
                "agent_call_id": None,
                "reason": "success",
            }

        except Exception as e:
            error_message = str(e)
            logger.error(
                f"[Transfer] Unexpected error during MPC transfer "
                f"'{mpc_name}': {error_message}",
                exc_info=True,
            )
            return {
                "success": False,
                "conference_id": mpc_name,
                "agent_call_id": None,
                "reason": "mpc_transfer_api_error",
                "error": error_message,
            }

    # ------------------------------------------------------------------
    # Legacy immediate transfer (native calls.transfer + <Dial> XML)
    # ------------------------------------------------------------------

    async def _transfer_call(
        self,
        customer_call_sid: str,
        telephony_number: str,
        status_callback_url: Optional[str] = None,
    ) -> Dict:
        """
        Transfer the customer's live call via the Plivo native Transfer API.

        The ``aleg_url`` points to a callback that returns ``<Dial><Number>``
        XML to dial the agent.
        """
        answer_url = _dial_up_url(
            customer_call_sid=customer_call_sid,
            telephony_number=telephony_number,
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

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, partial(self.client.calls.transfer, **transfer_kwargs)
            )

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
            logger.opt(exception=e).warning(
                f"[Transfer] Customer-leg transfer API failed "
                f"for call {customer_call_sid}"
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
        Execute a legacy immediate transfer via the Plivo native Transfer API.

        This delegates to ``client.calls.transfer()``, transferring the
        customer's live leg to the dial-up callback endpoint which returns
        ``<Dial><Number>`` XML to connect the agent to the call. The bot's
        ``<Stream>`` ends as soon as the customer leg is transferred — the
        caller is expected to end the conversation right after.

        Returns dict matching the base-provider contract::

            {
                "success": bool,
                "conference_id": str,   # conference name (for compat)
                "agent_call_id": str,   # agent leg call UUID
                "reason": str,
            }
        """
        logger.info(
            f"[Transfer] Initiating legacy transfer "
            f"'{conference_name}' — agent={agent_phone_number}, "
            f"customer_call_sid={customer_call_sid}"
        )

        try:
            # Transfer customer leg
            transfer_result = await self._transfer_call(
                customer_call_sid=customer_call_sid,
                telephony_number=telephony_number,
                status_callback_url=status_callback_url,
            )

            if not transfer_result["success"]:
                return {
                    **transfer_result,
                    "conference_id": conference_name,
                    "agent_call_id": None,
                }

            agent_call_uuid = transfer_result["agent_call_uuid"]
            logger.info(
                f"[Transfer] Customer call {customer_call_sid} successfully "
                f"transferred. Agent will be dialled."
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
                f"[Transfer] Unexpected error during legacy transfer "
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
