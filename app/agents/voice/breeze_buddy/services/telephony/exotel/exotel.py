import json

import requests
from fastapi import HTTPException, WebSocket
from pipecat.serializers.exotel import ExotelFrameSerializer

from app.agents.voice.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.agents.voice.breeze_buddy.workflows.order_confirmation.websocket_bot import (
    main as telephony_websocket_conn,
)
from app.core import config
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config
from app.schemas import CallProvider


class ExotelProvider(VoiceCallProvider):
    def __init__(self, aiohttp_session):
        super().__init__(config, aiohttp_session)

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        serializer = lambda stream_sid, call_sid: ExotelFrameSerializer(
            stream_sid=stream_sid,
            call_sid=call_sid,
        )
        await telephony_websocket_conn(
            websocket,
            self.aiohttp_session,
            serializer,
            None,
            self.completion_callback,
            provider,
        )

    def make_call(self, customer_mobile_number: str, outbound_number: str):
        flow_url = f"http://my.exotel.com/{self.config.EXOTEL_ACCOUNT_SID}/exoml/start_voice/{self.config.EXOTEL_APPLET_APP_ID}"

        payload = {
            "From": customer_mobile_number,
            "CallerId": outbound_number,
            "Url": flow_url,
            "StatusCallback": (
                self.config.APP_BASE_URL
                + "/agent/voice/breeze-buddy/exotel/callback/status"
            ),
        }
        url = f"https://{self.config.EXOTEL_API_KEY}:{self.config.EXOTEL_API_TOKEN}@{self.config.EXOTEL_SUBDOMAIN}/v1/Accounts/{self.config.EXOTEL_ACCOUNT_SID}/Calls/connect.json"

        logger.info(f"Making Exotel API call to: {self.config.EXOTEL_SUBDOMAIN}")
        logger.info(f"Payload: {payload}")

        try:
            # Use centralized proxy configuration
            proxy_url = get_proxy_config()
            proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None

            resp = requests.post(url, data=payload, proxies=proxies)
            logger.info(f"Exotel API response status: {resp.status_code}")
            logger.info(f"Exotel API response headers: {dict(resp.headers)}")
            logger.info(f"Exotel API response content: {resp.text}")

            if not resp.ok:
                logger.error(f"Exotel API error: {resp.status_code} - {resp.text}")
                raise HTTPException(resp.status_code, resp.text)

            # Check if response has content
            if not resp.text.strip():
                logger.warning("Exotel API returned empty response")
                return {
                    "status": "success",
                    "message": "Call initiated successfully",
                    "response": "",
                }

            # Parse JSON response
            try:
                response_json = resp.json()
                sid = response_json.get("Call", {}).get("Sid")
                if sid:
                    return {"status": "call_initiated", "sid": sid}
                else:
                    logger.error("Could not find 'Sid' in Exotel API response")
                    return {
                        "status": "error",
                        "message": "Could not find 'Sid' in response",
                        "response": resp.text,
                    }
            except json.JSONDecodeError as json_err:
                logger.error(f"Failed to parse JSON response: {json_err}")
                logger.error(f"Response content: {resp.text}")
                return {
                    "status": "error",
                    "message": "Failed to parse JSON response",
                    "response": resp.text,
                }

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
