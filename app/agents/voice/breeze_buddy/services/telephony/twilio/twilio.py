from fastapi import WebSocket, HTTPException
from twilio.rest import Client
from twilio.http.http_client import TwilioHttpClient
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.agents.voice.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.core import config
from app.core.transport.http_client import get_proxy_config
from app.agents.voice.breeze_buddy.workflows.order_confirmation.websocket_bot import (
    main as telephony_websocket_conn,
)
from pipecat.serializers.twilio import TwilioFrameSerializer
from app.core.logger import logger
from app.schemas import CallProvider


class TwilioProvider(VoiceCallProvider):
    class CustomTwilioFrameSerializer(TwilioFrameSerializer):
        async def _hang_up_call(self):
            logger.info("Skipping automatic hang-up from serializer.")

    def __init__(self, aiohttp_session):
        super().__init__(config, aiohttp_session)

        # Create Twilio client with proper proxy configuration
        self.client = self._create_twilio_client()

    def _create_twilio_client(self) -> Client:
        """Create Twilio client with proper proxy configuration using TwilioHttpClient"""
        proxy_url = get_proxy_config()
        account_sid = self.config.TWILIO_ACCOUNT_SID
        auth_token = self.config.TWILIO_AUTH_TOKEN

        if proxy_url:
            logger.info(f"Configuring Twilio client with proxy: {proxy_url}")
            # Use TwilioHttpClient with proxy configuration
            proxy_client = TwilioHttpClient(
                proxy={
                    "http": proxy_url,
                    "https": proxy_url,
                }
            )
            return Client(account_sid, auth_token, http_client=proxy_client)
        else:
            logger.info("Creating Twilio client without proxy")
            return Client(account_sid, auth_token)

    def hangup_call(self, call_sid: str):
        self.client.calls(call_sid).update(status="completed")

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        serializer = lambda stream_sid, call_sid: self.CustomTwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=self.config.TWILIO_ACCOUNT_SID,
            auth_token=self.config.TWILIO_AUTH_TOKEN,
        )
        await telephony_websocket_conn(
            websocket,
            self.aiohttp_session,
            serializer,
            self.hangup_call,
            self.completion_callback,
            provider,
        )

    def make_call(self, customer_mobile_number: str, outbound_number: str):
        ws_url = self.config.TWILIO_WEBSOCKET_URL

        voice_call_payload = VoiceResponse()
        connect = Connect()
        stream = Stream(url=ws_url)
        connect.append(stream)
        voice_call_payload.append(connect)

        try:
            call = self.client.calls.create(
                to=customer_mobile_number,
                from_=outbound_number,
                twiml=str(voice_call_payload),
                status_callback=(
                    self.config.APP_BASE_URL
                    + "/agent/voice/breeze-buddy/twilio/callback/status"
                ),
            )
            return {"status": "call_initiated", "sid": call.sid}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
