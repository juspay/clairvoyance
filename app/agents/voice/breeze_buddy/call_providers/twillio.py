from fastapi import WebSocket, HTTPException
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.agents.voice.breeze_buddy.call_providers.main import VoiceCallProvider
from app.core import config
from app.schemas import CallDataResponse
from app.agents.voice.breeze_buddy.breeze.order_confirmation.websocket_bot import main as telephony_websocket_conn
from pipecat.serializers.twilio import TwilioFrameSerializer
from loguru import logger


class TwilioProvider(VoiceCallProvider):
    class CustomTwilioFrameSerializer(TwilioFrameSerializer):
        async def _hang_up_call(self):
            logger.info("Skipping automatic hang-up from serializer.")
            pass
    def __init__(self, aiohttp_session):
        super().__init__(config, aiohttp_session)
        if not all([self.config.TWILIO_ACCOUNT_SID, self.config.TWILIO_AUTH_TOKEN, self.config.TWILIO_FROM_NUMBER]):
            raise ValueError("Twilio credentials are not configured.")
        self.client = Client(self.config.TWILIO_ACCOUNT_SID, self.config.TWILIO_AUTH_TOKEN)

    def hangup_call(self, call_sid: str):
        self.client.calls(call_sid).update(status="completed")

    async def handle_websocket(self, websocket: WebSocket):
        serializer = lambda stream_sid, call_sid: self.CustomTwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=self.config.TWILIO_ACCOUNT_SID,
            auth_token=self.config.TWILIO_AUTH_TOKEN,
        )
        await telephony_websocket_conn(websocket, self.aiohttp_session, serializer, self.hangup_call, self.completion_callback)

    def make_call(self, call_data: CallDataResponse):
        ws_url = self.config.TWILIO_WEBSOCKET_URL

        voice_call_payload = VoiceResponse()
        connect = Connect()
        stream = Stream(url=ws_url)
        connect.append(stream)
        voice_call_payload.append(connect)

        try:
            call = self.client.calls.create(
                to=call_data.call_payload.get("customer_mobile_number"),
                from_=self.config.TWILIO_FROM_NUMBER,
                twiml=str(voice_call_payload)
            )
            return {"status": "call_initiated", "sid": call.sid}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
