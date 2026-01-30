from fastapi import WebSocket
from pipecat.serializers.twilio import TwilioFrameSerializer
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.websocket_bot import (
    main as telephony_websocket_conn,
)
from app.core.config.static import (
    APP_BASE_URL,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_TEMPLATE_WEBSOCKET_URL,
    TWILIO_WEBSOCKET_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config
from app.schemas import CallProvider


class TwilioProvider(VoiceCallProvider):
    class CustomTwilioFrameSerializer(TwilioFrameSerializer):
        async def _hang_up_call(self):
            logger.info("Skipping automatic hang-up from serializer.")

    def __init__(self, aiohttp_session, use_template_flow: bool = False):
        # Store config values directly as instance attributes
        self.TWILIO_ACCOUNT_SID = TWILIO_ACCOUNT_SID
        self.TWILIO_AUTH_TOKEN = TWILIO_AUTH_TOKEN
        self.TWILIO_WEBSOCKET_URL = TWILIO_WEBSOCKET_URL
        self.TWILIO_TEMPLATE_WEBSOCKET_URL = TWILIO_TEMPLATE_WEBSOCKET_URL
        self.APP_BASE_URL = APP_BASE_URL
        self.use_template_flow = use_template_flow

        # Call parent without config object
        super().__init__(None, aiohttp_session)

        # Create Twilio client with proper proxy configuration
        self.client = self._create_twilio_client()

    def _create_twilio_client(self) -> Client:
        """Create Twilio client with proper proxy configuration using TwilioHttpClient"""
        proxy_url = get_proxy_config()
        account_sid = self.TWILIO_ACCOUNT_SID
        auth_token = self.TWILIO_AUTH_TOKEN

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
            account_sid=self.TWILIO_ACCOUNT_SID,
            auth_token=self.TWILIO_AUTH_TOKEN,
        )
        if self.use_template_flow:
            logger.info("Using template flow for Twilio WebSocket connection")
            await telephony_bot(
                websocket,
                self.aiohttp_session,
                self.hangup_call,
                self.completion_callback,
                provider,
            )
        else:
            logger.info("Using standard flow for Twilio WebSocket connection")
            await telephony_websocket_conn(
                websocket,
                self.aiohttp_session,
                serializer,
                self.hangup_call,
                self.completion_callback,
                provider,
            )

    def make_call(self, customer_mobile_number: str, outbound_number: str):
        ws_url = (
            self.TWILIO_TEMPLATE_WEBSOCKET_URL
            if self.use_template_flow
            else self.TWILIO_WEBSOCKET_URL
        )

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
                record=True,
                recording_status_callback=(
                    self.APP_BASE_URL
                    + "/agent/voice/breeze-buddy/twilio/callback/details"
                ),
                status_callback=(
                    self.APP_BASE_URL
                    + "/agent/voice/breeze-buddy/twilio/callback/status"
                ),
            )
            return {"status": "call_initiated", "sid": call.sid}
        except Exception as e:
            logger.error(f"Error when making call via Twilio: {e}")
            return None
