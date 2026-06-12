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
from typing import Dict

import plivo

from app.core.config.static import APP_BASE_URL
from app.core.logger import logger


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
        outbound_number: str,
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
                    from_=outbound_number,
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

    async def handle_transfer(
        self,
        conference_name: str,
        agent_phone_number: str,
        customer_call_sid: str,
        outbound_number: str,
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
                outbound_number=outbound_number,
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
