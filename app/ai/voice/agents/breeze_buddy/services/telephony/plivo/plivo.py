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
        outbound_number: str,
        reseller_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
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
            outbound_number: Caller ID / outbound number
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
                from_=outbound_number,
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
    """Build <Dial><Number> XML that bridges customer → agent."""
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

    outbound_number = params.get("outbound_number", "")
    action_url = (
        f"{APP_BASE_URL}/agent/voice/breeze-buddy"
        f"/plivo/callback/transfer/conclude"
        f"?customer_call_sid={call_sid}"
    )
    xml = (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>"
        f'<Dial action="{action_url}" method="POST"'
        f' callerId="{outbound_number}" timeout="30">'
        f"<Number>{agent_phone}</Number>"
        f"</Dial></Response>"
    )
    return HTMLResponse(content=xml, media_type="application/xml")
