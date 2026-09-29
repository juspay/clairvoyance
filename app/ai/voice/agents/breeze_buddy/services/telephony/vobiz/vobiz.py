"""
Vobiz telephony provider.

Vobiz's REST API is shaped like Plivo's (same /Account/{auth_id}/Call/ path
and parameter names) but lives on its own host and authenticates with
X-Auth-ID / X-Auth-Token headers, so the plivo SDK cannot be pointed at it.
Transfers are not supported yet: conference_service stays None, which the
transfer handlers already treat as "unavailable".
"""

from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests
from fastapi import WebSocket

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.core.config.static import (
    APP_BASE_URL,
    VOBIZ_API_BASE_URL,
    VOBIZ_AUTH_ID,
    VOBIZ_AUTH_TOKEN,
)
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config
from app.schemas import CallProvider, TelephonyConfig

# make_call runs in a worker thread (make_call_async); a black-holed
# connection must not hang pod shutdown (same reasoning as ExotelProvider).
_REQUEST_TIMEOUT_SECONDS = 30


class VobizProvider(VoiceCallProvider):
    def __init__(
        self, aiohttp_session, telephony_config: Optional[TelephonyConfig] = None
    ):
        super().__init__(None, aiohttp_session, telephony_config)

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        logger.info("Using template flow for Vobiz WebSocket connection")
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
        telephony_number: str,
        reseller_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Initiate an outbound call via Vobiz.

        The answer_url points to /vobiz/answer (lead lookup, pod allocation,
        recording + stream XML); hangup_url is the shared status callback.

        Returns None when Vobiz did not place the call (transport error or a
        non-2xx such as 401/402/429), which the dispatch worker reads as
        "nothing rang" and un-records the attempt. A 2xx without a
        request_uuid may have rung, so it returns a dict without "sid".
        """
        answer_url = f"{APP_BASE_URL}/agent/voice/breeze-buddy/vobiz/answer"
        params = {}
        if reseller_id:
            params["reseller_id"] = reseller_id
        if template_name:
            params["template"] = template_name
        if params:
            answer_url += "?" + urlencode(params)

        payload = {
            "from": telephony_number,
            "to": customer_mobile_number,
            "answer_url": answer_url,
            "answer_method": "POST",
            "hangup_url": f"{APP_BASE_URL}/agent/voice/breeze-buddy/vobiz/callback/status",
            "hangup_method": "POST",
        }
        url = f"{VOBIZ_API_BASE_URL}/Account/{VOBIZ_AUTH_ID}/Call/"
        headers = {"X-Auth-ID": VOBIZ_AUTH_ID, "X-Auth-Token": VOBIZ_AUTH_TOKEN}
        proxy_url = get_proxy_config()
        proxies = {"https": proxy_url, "http": proxy_url} if proxy_url else None

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                proxies=proxies,
                timeout=_REQUEST_TIMEOUT_SECONDS,
                # A followed redirect would carry X-Auth-* to wherever it
                # points (requests strips only Authorization on a host change).
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as e:
            logger.error(f"Error when making call via Vobiz: {e}")
            return None

        # Only a 2xx placed the call; a 3xx was not followed, so nothing rang.
        if not 200 <= resp.status_code < 300:
            logger.error(f"Vobiz make_call failed: {resp.status_code} - {resp.text}")
            return None

        try:
            body = resp.json()
        except ValueError:
            body = None
        call_uuid = body.get("request_uuid") if isinstance(body, dict) else None

        if not isinstance(call_uuid, str) or not call_uuid:
            logger.error(f"Vobiz make_call 2xx without request_uuid: {resp.text}")
            return {
                "status": "error",
                "message": "No request_uuid",
                "response": resp.text,
            }

        logger.info(f"Vobiz call initiated with answer_url: {answer_url}")
        logger.info(f"Vobiz call initiated successfully: {call_uuid}")
        return {"status": "call_initiated", "sid": call_uuid}
