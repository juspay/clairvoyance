import json
from typing import Any, Dict, Optional

import requests
from fastapi import WebSocket

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.core.config.static import (
    APP_BASE_URL,
    EXOTEL_ACCOUNT_SID,
    EXOTEL_API_KEY,
    EXOTEL_API_TOKEN,
    EXOTEL_SUBDOMAIN,
    EXOTEL_TEMPLATE_APPLET_APP_ID,
)
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config
from app.schemas import CallProvider, TelephonyConfig


class ExotelProvider(VoiceCallProvider):
    def __init__(
        self, aiohttp_session, telephony_config: Optional[TelephonyConfig] = None
    ):
        # Store config values directly as instance attributes
        self.EXOTEL_ACCOUNT_SID = EXOTEL_ACCOUNT_SID
        self.EXOTEL_API_KEY = EXOTEL_API_KEY
        self.EXOTEL_API_TOKEN = EXOTEL_API_TOKEN
        self.EXOTEL_TEMPLATE_APPLET_APP_ID = EXOTEL_TEMPLATE_APPLET_APP_ID
        self.EXOTEL_SUBDOMAIN = EXOTEL_SUBDOMAIN
        self.APP_BASE_URL = APP_BASE_URL

        # Call parent with telephony_config
        super().__init__(None, aiohttp_session, telephony_config)

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        logger.info("Using template flow for Exotel WebSocket connection")
        await telephony_bot(
            websocket,
            self.aiohttp_session,
            self.completion_callback,
            provider,
        )

    def make_call(
        self,
        customer_mobile_number: str,
        outbound_number: str,
        merchant_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Initiate an outbound call via Exotel.

        Note: Exotel uses applet-based routing configured in their dashboard,
        so merchant_id and template_name are not used here directly (included
        for interface consistency). The template is resolved at answer-time
        via the voicebot-url webhook handler.
        """
        # Resolve applet app ID: telephony_config override > env default
        if self.telephony_config and self.telephony_config.applet_app_id:
            applet_id = self.telephony_config.applet_app_id
            logger.info(f"Using applet_app_id from telephony_config: {applet_id}")
        else:
            applet_id = self.EXOTEL_TEMPLATE_APPLET_APP_ID
            logger.info(f"Using applet_app_id from env default: {applet_id}")

        flow_url = f"http://my.exotel.com/{self.EXOTEL_ACCOUNT_SID}/exoml/start_voice/{applet_id}"

        payload = {
            "From": customer_mobile_number,
            "CallerId": outbound_number,
            "Url": flow_url,
            "StatusCallback": (
                self.APP_BASE_URL + "/agent/voice/breeze-buddy/exotel/callback/status"
            ),
        }

        url = f"https://{self.EXOTEL_API_KEY}:{self.EXOTEL_API_TOKEN}@{self.EXOTEL_SUBDOMAIN}/v1/Accounts/{self.EXOTEL_ACCOUNT_SID}/Calls/connect.json"

        logger.info(f"Making Exotel API call to: {self.EXOTEL_SUBDOMAIN}")
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
                return None

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
            return None
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout error when calling Exotel API: {e}")
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Request error when calling Exotel API: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error when calling Exotel API: {e}")
            return None
