from fastapi import WebSocket, HTTPException
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream

from app.agents.voice.breeze_buddy.services.telephony.base_provider import VoiceCallProvider
from app.core import config
from app.agents.voice.breeze_buddy.workflows.order_confirmation.websocket_bot import main as telephony_websocket_conn
from pipecat.serializers.twilio import TwilioFrameSerializer
from loguru import logger


class TwilioProvider(VoiceCallProvider):
    class CustomTwilioFrameSerializer(TwilioFrameSerializer):
        async def _hang_up_call(self):
            logger.info("Skipping automatic hang-up from serializer.")
            pass
    def __init__(self, aiohttp_session):
        super().__init__(config, aiohttp_session)
        self.client = Client(self.config.TWILIO_ACCOUNT_SID, self.config.TWILIO_AUTH_TOKEN)

    def hangup_call(self, call_sid: str):
        self.client.calls(call_sid).update(status="completed")

    async def handle_websocket(self, websocket: WebSocket, provider: str):
        serializer = lambda stream_sid, call_sid: self.CustomTwilioFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
            account_sid=self.config.TWILIO_ACCOUNT_SID,
            auth_token=self.config.TWILIO_AUTH_TOKEN,
        )
        await telephony_websocket_conn(websocket, self.aiohttp_session, serializer, self.hangup_call, self.completion_callback, provider)

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
                twiml=str(voice_call_payload)
            )
            return {"status": "call_initiated", "sid": call.sid}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
