import json
from typing import Optional
from urllib.parse import quote
from fastapi import WebSocket, HTTPException
from starlette.websockets import WebSocketState
import requests
from loguru import logger

from pipecat.transports.network.fastapi_websocket import (
    FastAPIWebsocketTransport, FastAPIWebsocketParams
)
from pipecat.serializers.exotel import ExotelFrameSerializer

from app.agents.voice.breeze_buddy.call_providers.main import VoiceCallProvider
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
        # URL encode parameters to handle special characters
        order_data_json = quote(json.dumps(order.order_data.model_dump()))
        customer_name = quote(str(order.customer_name))
        shop_name = quote(str(order.shop_name))
        customer_address = quote(str(order.customer_address))
        
        flow_url = f"{self.config.EXOTEL_ORDER_CONFIRMATION_WEBSOCKET_URL}?order_id={order.order_id}&customer_name={customer_name}&shop_name={shop_name}&total_price={order.total_price}&customer_address={customer_address}&customer_mobile_number={order.customer_mobile_number}&order_data={order_data_json}&identity=exotel"
        if order.reporting_webhook_url:
            flow_url += f"&reporting_webhook_url={quote(order.reporting_webhook_url)}"

        payload = {
            "From": order.customer_mobile_number,
            "CallerId": self.config.EXOTEL_FROM_NUMBER,
            "Url": flow_url
        }
        url = f"https://{self.config.EXOTEL_API_KEY}:{self.config.EXOTEL_API_TOKEN}@{self.config.EXOTEL_SUBDOMAIN}/v1/Accounts/{self.config.EXOTEL_ACCOUNT_SID}/Calls/connect"
        
        logger.info(f"Making Exotel API call to: {self.config.EXOTEL_SUBDOMAIN}")
        logger.info(f"Payload: {payload}")
        
        try:
            resp = requests.post(url, data=payload)
            logger.info(f"Exotel API response status: {resp.status_code}")
            logger.info(f"Exotel API response headers: {dict(resp.headers)}")
            logger.info(f"Exotel API response content: {resp.text}")
            
            if not resp.ok:
                logger.error(f"Exotel API error: {resp.status_code} - {resp.text}")
                raise HTTPException(resp.status_code, resp.text)
            
            # Check if response has content and is valid JSON
            if not resp.text.strip():
                logger.warning("Exotel API returned empty response")
                return {"status": "success", "message": "Call initiated successfully", "response": ""}
            
            # Check content type to ensure it's JSON
            content_type = resp.headers.get('content-type', '').lower()
            if 'application/json' not in content_type:
                logger.warning(f"Exotel API returned non-JSON content type: {content_type}")
                return {"status": "success", "message": "Call initiated successfully", "response": resp.text}
            
            try:
                return resp.json()
            except json.JSONDecodeError as json_err:
                logger.error(f"Failed to parse JSON response: {json_err}")
                logger.error(f"Response content: {resp.text}")
                return {"status": "success", "message": "Call initiated successfully", "response": resp.text}
                
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error when calling Exotel API: {e}")
            raise HTTPException(503, f"Failed to connect to Exotel API: {str(e)}")
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout error when calling Exotel API: {e}")
            raise HTTPException(504, f"Exotel API request timed out: {str(e)}")
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error when calling Exotel API: {e}")
            raise HTTPException(500, f"Request error: {str(e)}")
        except Exception as e:
            logger.error(f"Unexpected error when calling Exotel API: {e}")
            raise HTTPException(500, f"Unexpected error: {str(e)}")
