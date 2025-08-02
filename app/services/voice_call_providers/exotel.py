import json
from typing import Optional
from fastapi import WebSocket, HTTPException
from starlette.websockets import WebSocketState
import requests

from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketTransport, FastAPIWebsocketParams
)
from pipecat.serializers.exotel import ExotelFrameSerializer

from app.services.voice_call_providers.voice_call_provider import VoiceCallProvider
from app.core import config
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.agents.voice.breeze_buddy.breeze.order_confirmation.websocket_bot import main as telephony_websocket_conn


class ExotelProvider(VoiceCallProvider):
    def __init__(self, aiohttp_session):
        super().__init__(config, aiohttp_session)

    async def handle_websocket(self, websocket: WebSocket):
        serializer = lambda stream_id, call_sid: ExotelFrameSerializer(
            stream_id=stream_id,
            call_sid=call_sid,
        )
        await telephony_websocket_conn(websocket, self.aiohttp_session, serializer, None)

    def make_call(self, order: BreezeOrderData):
        flow_url = f"{self.config.TWILIO_WEBSOCKET_URL}/voice/exotel/ws?order_id={order.order_id}&customer_name={order.customer_name}&shop_name={order.shop_name}&total_price={order.total_price}&customer_address={order.customer_address}&customer_mobile_number={order.customer_mobile_number}&order_data={json.dumps(order.order_data)}&identity=exotel"
        if order.reporting_webhook_url:
            flow_url += f"&reporting_webhook_url={order.reporting_webhook_url}"

        payload = {
            "From": order.customer_mobile_number,
            "CallerId": self.config.EXOTEL_FROM_NUMBER,
            "Url": flow_url
        }
        url = f"https://{self.config.EXOTEL_API_KEY}:{self.config.EXOTEL_API_TOKEN}@{self.config.EXOTEL_ACCOUNT_SID}.{self.config.EXOTEL_SUBDOMAIN}/v1/Accounts/{self.config.EXOTEL_ACCOUNT_SID}/Calls/connect"
        resp = requests.post(url, data=payload)
        if not resp.ok:
            raise HTTPException(resp.status_code, resp.text)
        return resp.json()
