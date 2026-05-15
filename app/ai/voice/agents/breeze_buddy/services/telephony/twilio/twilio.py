from typing import Any, Dict, Optional
from urllib.parse import urlencode

from fastapi import WebSocket
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client
from twilio.twiml.voice_response import Connect, Stream, VoiceResponse

from app.ai.voice.agents.breeze_buddy.agent import telephony_bot
from app.ai.voice.agents.breeze_buddy.services.telephony.base_provider import (
    VoiceCallProvider,
)
from app.ai.voice.agents.breeze_buddy.services.telephony.twilio.conference import (
    TwilioConferenceService,
)
from app.core.config.static import (
    APP_BASE_URL,
    ENABLE_VOICE_AGENT_POD_ISOLATION,
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_TEMPLATE_WEBSOCKET_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import get_proxy_config
from app.schemas import CallProvider, TelephonyConfig


class TwilioProvider(VoiceCallProvider):
    def __init__(
        self, aiohttp_session, telephony_config: Optional[TelephonyConfig] = None
    ):
        # Store config values directly as instance attributes
        self.TWILIO_ACCOUNT_SID = TWILIO_ACCOUNT_SID
        self.TWILIO_AUTH_TOKEN = TWILIO_AUTH_TOKEN
        self.TWILIO_TEMPLATE_WEBSOCKET_URL = TWILIO_TEMPLATE_WEBSOCKET_URL
        self.APP_BASE_URL = APP_BASE_URL

        # Call parent with telephony_config
        super().__init__(None, aiohttp_session, telephony_config)

        # Create Twilio client with proper proxy configuration
        self.client = self._create_twilio_client()

        # Initialize conference service
        self.conference_service = TwilioConferenceService(self.client)

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

    async def handle_websocket(self, websocket: WebSocket, provider: CallProvider):
        logger.info("Using template flow for Twilio WebSocket connection")
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
        outbound_number: str,
        reseller_id: Optional[str] = None,
        merchant_id: Optional[str] = None,
        template_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Initiate an outbound call via Twilio.

        When pod isolation is enabled:
        - Uses url= webhook pointing to Smart Router (/api/v1/twilio/allocate).
          Twilio calls this webhook ONLY when the customer answers, so the pod
          is allocated at answer time (not at ring time). Smart Router returns
          TwiML with a pod-specific WebSocket URL.
        - When merchant_id is provided, uses /api/v1/twilio/merchant-allocate
          to route through the merchant-specific pod pool (with fallback).

        When pod isolation is disabled:
        - Uses twiml= with inline TwiML containing a static WebSocket URL.
          This is the original behavior before pod isolation.

        Args:
            customer_mobile_number: Phone number to call
            outbound_number: Caller ID / outbound number
            reseller_id: Optional reseller ID for tiered pod allocation
            merchant_id: Optional merchant ID — routes to dedicated merchant pool
            template_name: Optional template name for WebSocket path routing
        """
        try:
            if ENABLE_VOICE_AGENT_POD_ISOLATION:
                # Pod isolation ON: Twilio webhook points directly to Smart Router's
                # allocate endpoint via nginx pass-through (/api/v1/* → smart-router).
                # Smart Router allocates a pod and returns TwiML with pod-specific ws_url.
                flow = "v2"
                query_params = {"flow": flow}
                if reseller_id:
                    query_params["reseller_id"] = reseller_id
                if template_name:
                    query_params["template"] = template_name

                # Use merchant-specific endpoint when merchant_id is available.
                # This endpoint allocates from the merchant's dedicated pod pool
                # with 3-step fallback (merchant → standard → shared).
                if merchant_id:
                    query_params["merchant_id"] = merchant_id
                    allocate_path = "/api/v1/twilio/merchant-allocate"
                else:
                    allocate_path = "/api/v1/twilio/allocate"

                webhook_url = f"{self.APP_BASE_URL}{allocate_path}?" + urlencode(
                    query_params
                )

                # Fallback URL: if Smart Router is unreachable, Twilio hits
                # this endpoint which returns TwiML with static WebSocket URL.
                # This ensures the call still works without pod isolation.
                fallback_url = (
                    self.APP_BASE_URL
                    + "/agent/voice/breeze-buddy/twilio/callback/twiml-fallback"
                )

                call = self.client.calls.create(
                    to=customer_mobile_number,
                    from_=outbound_number,
                    url=webhook_url,
                    fallback_url=fallback_url,
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
                logger.info(
                    f"Twilio call initiated with Smart Router webhook: {webhook_url}"
                )
            else:
                # Pod isolation OFF: Use inline TwiML with static WebSocket URL.
                # Original behavior — no pod allocation needed.
                ws_url = self.TWILIO_TEMPLATE_WEBSOCKET_URL

                voice_call_payload = VoiceResponse()
                connect = Connect()
                stream = Stream(url=ws_url)
                connect.append(stream)
                voice_call_payload.append(connect)

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
                logger.info(
                    f"Twilio call initiated with inline TwiML, ws_url: {ws_url}"
                )

            return {"status": "call_initiated", "sid": call.sid}
        except Exception as e:
            logger.error(f"Error when making call via Twilio: {e}")
            return None
