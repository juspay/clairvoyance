import plivo
from fastapi import WebSocket
from pipecat.serializers.plivo import PlivoFrameSerializer

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.websocket_bot import (
    main as telephony_websocket_conn,
)
from app.core.config.static import APP_BASE_URL, PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN
from app.core.logger import logger
from app.schemas import CallProvider


class PlivoProvider(VoiceCallProvider):
    class CustomPlivoFrameSerializer(PlivoFrameSerializer):
        async def _hang_up_call(self):
            logger.info("Skipping automatic hang-up from serializer.")

    def __init__(self, aiohttp_session, use_template_flow: bool = False):
        # Store config values directly as instance attributes
        self.PLIVO_AUTH_ID = PLIVO_AUTH_ID
        self.PLIVO_AUTH_TOKEN = PLIVO_AUTH_TOKEN
        self.APP_BASE_URL = APP_BASE_URL
        self.use_template_flow = use_template_flow

        # Call parent without config object
        super().__init__(None, aiohttp_session)

        # Create Plivo client
        self.client = plivo.RestClient(self.PLIVO_AUTH_ID, self.PLIVO_AUTH_TOKEN)

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        serializer = lambda stream_id, call_id: self.CustomPlivoFrameSerializer(
            stream_id=stream_id,
            call_id=call_id,
            auth_id=self.PLIVO_AUTH_ID,
            auth_token=self.PLIVO_AUTH_TOKEN,
        )
        if self.use_template_flow:
            logger.info("Using template flow for Plivo WebSocket connection")
            await telephony_bot(
                websocket,
                self.aiohttp_session,
                None,
                self.completion_callback,
                provider,
            )
        else:
            logger.info("Using standard flow for Plivo WebSocket connection")
            await telephony_websocket_conn(
                websocket,
                self.aiohttp_session,
                serializer,
                None,
                self.completion_callback,
                provider,
            )

    def make_call(self, customer_mobile_number: str, outbound_number: str):
        """Initiate an outbound call via Plivo."""
        try:
            # Create the call using Plivo's API
            response = self.client.calls.create(
                from_=outbound_number,
                to_=customer_mobile_number,
                answer_url=f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/answer",
                hangup_url=f"{self.APP_BASE_URL}/agent/voice/breeze-buddy/plivo/callback/status",
            )

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
