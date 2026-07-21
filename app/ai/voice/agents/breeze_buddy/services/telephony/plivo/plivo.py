from typing import Optional
from urllib.parse import urlencode

import plivo
from fastapi import WebSocket
from starlette.responses import HTMLResponse

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.conference import (
    PlivoConferenceService,
)
from app.ai.voice.agents.breeze_buddy.utils.hold_transfer import (
    publish_hold_transfer_result,
)
from app.core.config.static import (
    APP_BASE_URL,
    PLIVO_AUTH_ID,
    PLIVO_AUTH_TOKEN,
)
from app.core.logger import logger
from app.schemas import CallProvider, TelephonyConfig


class PlivoProvider(VoiceCallProvider):
    def __init__(
        self, aiohttp_session, telephony_config: Optional[TelephonyConfig] = None
    ):
        # Store config values directly as instance attributes
        self.PLIVO_AUTH_ID = PLIVO_AUTH_ID
        self.PLIVO_AUTH_TOKEN = PLIVO_AUTH_TOKEN
        self.APP_BASE_URL = APP_BASE_URL

        super().__init__(None, aiohttp_session, telephony_config)

        # Create Plivo client
        self.client = plivo.RestClient(self.PLIVO_AUTH_ID, self.PLIVO_AUTH_TOKEN)

        # Initialize conference service for transfers
        self.conference_service = PlivoConferenceService(self.client)

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        logger.info("Using template flow for Plivo WebSocket connection")
        await telephony_bot(
            websocket,
            self.aiohttp_session,
            self.completion_callback,
            provider,
            telephony_service=self,
        )

    def make_call(
        self,
        customer_mobile_number: str,
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ):
        """
        Initiate an outbound call via Plivo.

        The answer_url always points to /plivo/answer which handles:
        - Starting call recording via Plivo API
        - Noise cancellation configuration
        - Pod allocation via Smart Router (when pod isolation is enabled)
        - Returning XML with WebSocket URL

        Args:
            customer_mobile_number: Phone number to call
            telephony_number: Caller ID / outbound number
            reseller_id: Optional merchant ID for tiered pod allocation
            template_name: Optional template name for WebSocket path routing
        """
        answer_url = f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/answer"
        params = {}
        if reseller_id:
            params["reseller_id"] = reseller_id
        if template_name:
            params["template"] = template_name
        if params:
            answer_url += "?" + urlencode(params)

        try:
            response = self.client.calls.create(
                from_=telephony_number,
                to_=customer_mobile_number,
                answer_url=answer_url,
                hangup_url=f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/callback/status",
            )

            logger.info(f"Plivo call initiated with answer_url: {answer_url}")
            logger.info(f"Plivo call response: {response}")

            # Get the call UUID from the response
            call_uuid = None
            if hasattr(response, "request_uuid"):
                call_uuid = response.request_uuid
            elif hasattr(response, "call_uuid"):
                call_uuid = response.call_uuid
            elif hasattr(response, "api_id"):
                call_uuid = response.api_id

            logger.info(f"Plivo call initiated successfully: {call_uuid}")
            return {"status": "call_initiated", "sid": call_uuid}

        except Exception as e:
            logger.error(f"Error when making call via Plivo: {e}")
            return None


async def plivo_dial_xml(
    transfer_data: dict, call_sid: str, params: dict
) -> HTMLResponse:
    """Build <Dial><Number> XML that bridges customer → agent (legacy transfer).

    Used by the immediate (non-MPC) transfer path: Plivo fetches this from the
    dial-up callback after the customer's leg has been transferred, and the XML
    dials the agent from the customer's leg.
    """
    transfer_number = transfer_data.get("transfer_number")
    if not transfer_number:
        logger.error(f"[TRANSFER DIAL-UP] No transfer_number for call {call_sid}")
        return HTMLResponse(
            content='<?xml version="1.0" encoding="UTF-8"?>'
            "<Response><Speak>Sorry, the transfer could not be completed.</Speak>"
            "<Hangup/></Response>",
            media_type="application/xml",
        )

    agent_phone = transfer_number
    if not agent_phone.startswith("+"):
        agent_phone = f"+{agent_phone}"

    telephony_number = params.get("outbound_number", "")
    action_url = (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy"
        f"/plivo/callback/transfer/conclude"
        f"?customer_call_sid={call_sid}"
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>"
        f'<Dial action="{action_url}" method="POST"'
        f' callerId="{telephony_number}" timeout="30">'
        f"<Number>{agent_phone}</Number>"
        f"</Dial></Response>"
    )
    return HTMLResponse(content=xml, media_type="application/xml")


async def handle_mpc_transfer_webhook(params: dict) -> None:
    """Handle Plivo MPC participant-state-changes webhook.

    Fires when an MPC participant's state changes (joins / exits).
    Used to detect whether the agent answered the transfer call.

    Flow:
      - Agent answers → ParticipantJoin (role=agent)
          → move customer into MPC, publish "answered" to Redis
      - Agent no-answer → ParticipantExit (role=agent)
          → publish "unavailable" to Redis
    """
    call_sid = str(params.get("call_sid") or "")
    event = str(params.get("EventName", ""))
    mpc_name = str(params.get("MPCName", ""))
    participant_role = str(params.get("ParticipantRole", "")).lower()

    logger.info(
        f"[MPC-TRANSFER] event={event} role={participant_role} "
        f"mpc={mpc_name} call_sid={call_sid}"
    )

    if not call_sid:
        logger.error("[MPC-TRANSFER] Missing call_sid in callback")
        return

    outcome_channel = f"transfer_outcome:{call_sid}"

    if participant_role == "agent" and event == "ParticipantJoin":
        logger.info(
            f"[MPC-TRANSFER] Agent answered for call {call_sid}. "
            f"Moving customer into MPC '{mpc_name}'."
        )

        client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
        conference_service = PlivoConferenceService(client)
        result = await conference_service.move_customer_to_mpc(
            call_sid=call_sid,
            mpc_name=mpc_name,
        )

        if result["success"]:
            await publish_hold_transfer_result(
                outcome_channel,
                {"status": "answered"},
            )
            logger.info(
                f"[MPC-TRANSFER] Customer moved to MPC, "
                f"published 'answered' for {call_sid}"
            )
        else:
            logger.error(
                f"[MPC-TRANSFER] Failed to move customer to MPC: "
                f"{result.get('error')}"
            )
            await publish_hold_transfer_result(
                outcome_channel,
                {
                    "status": "unavailable",
                    "reason": "mpc_move_failed",
                    "error": result.get("error"),
                },
            )

    elif participant_role == "agent" and event == "ParticipantExit":
        # Agent never joined → timed out / busy → publish "unavailable".
        # If ParticipantJoinTime is present, the agent had already joined
        # and the transfer succeeded — this exit is a normal hang-up after
        # a completed transfer and must not publish "unavailable".
        join_time = str(params.get("ParticipantJoinTime", ""))
        if join_time:
            logger.info(
                f"[MPC-TRANSFER] Agent exited after joining for call {call_sid}. "
                f"Transfer was successful — not publishing 'unavailable'."
            )
        else:
            logger.info(
                f"[MPC-TRANSFER] Agent exited without joining for call {call_sid}. "
                f"Publishing 'unavailable'."
            )
            await publish_hold_transfer_result(
                outcome_channel,
                {"status": "unavailable"},
            )
