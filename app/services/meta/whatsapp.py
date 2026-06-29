"""Meta Graph API client for WhatsApp Embedded Signup onboarding."""

from typing import Any, Dict, Optional

import httpx

from app.core.config.static import (
    META_APP_ID,
    META_APP_SECRET,
    META_GRAPH_API_VERSION,
    META_GRAPH_BASE_URL,
    META_REQUEST_TIMEOUT_SECONDS,
    META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID,
    WHATSAPP_PAYMENT_LINK_TEMPLATE_CATEGORY,
    WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE,
    WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME,
)
from app.core.transport.http_client import create_http_client
from app.schemas.breeze_buddy.whatsapp import (
    MetaMessageSendResult,
    MetaTemplateCreateResult,
    MetaTokenExchangeResult,
)


class MetaWhatsAppConfigurationError(RuntimeError):
    """Raised when Meta WhatsApp env config is missing."""


class MetaWhatsAppAPIError(RuntimeError):
    """Raised when Meta Graph API returns an error."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        error_code: Optional[str] = None,
        error_subcode: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.error_subcode = error_subcode


def normalize_graph_api_version(version: str) -> str:
    """Return Graph API version with a leading 'v'."""

    version = (version or "").strip()
    if not version:
        return "v25.0"
    return version if version.startswith("v") else f"v{version}"


class MetaWhatsAppClient:
    """Small async Meta Graph client for onboarding calls."""

    def __init__(
        self,
        app_id: str = META_APP_ID,
        app_secret: str = META_APP_SECRET,
        embedded_signup_config_id: str = META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID,
        graph_api_version: str = META_GRAPH_API_VERSION,
        graph_base_url: str = META_GRAPH_BASE_URL,
        timeout_seconds: int = META_REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self._embedded_signup_config_id = embedded_signup_config_id
        self.graph_api_version = normalize_graph_api_version(graph_api_version)
        self.graph_base_url = graph_base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    @property
    def embedded_signup_config_id(self) -> str:
        """Configured Facebook Login for Business configuration ID."""

        return self._embedded_signup_config_id

    def ensure_configured(self) -> None:
        """Validate required env config before launching or completing signup."""

        missing = []
        if not self.app_id:
            missing.append("META_APP_ID")
        if not self.app_secret:
            missing.append("META_APP_SECRET")
        if not self.embedded_signup_config_id:
            missing.append("META_WHATSAPP_EMBEDDED_SIGNUP_CONFIG_ID")
        if missing:
            raise MetaWhatsAppConfigurationError(
                f"Missing Meta WhatsApp configuration: {', '.join(missing)}"
            )

    async def exchange_code_for_business_token(
        self, code: str
    ) -> MetaTokenExchangeResult:
        """Exchange Meta's short-lived code for a business token."""

        self.ensure_configured()
        data = await self._request_json(
            method="GET",
            path="/oauth/access_token",
            params={
                "client_id": self.app_id,
                "client_secret": self.app_secret,
                "code": code,
            },
        )
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise MetaWhatsAppAPIError("Meta token exchange did not return a token")

        expires_in = data.get("expires_in")
        return MetaTokenExchangeResult(
            access_token=access_token,
            token_type=data.get("token_type"),
            expires_in=expires_in if isinstance(expires_in, int) else None,
            scope=data.get("scope"),
            raw_metadata={
                key: value for key, value in data.items() if key != "access_token"
            },
        )

    async def subscribe_app_to_waba(
        self,
        waba_id: str,
        business_token: str,
    ) -> bool:
        """Subscribe this app to webhooks on the customer's WABA."""

        self.ensure_configured()
        data = await self._request_json(
            method="POST",
            path=f"/{waba_id}/subscribed_apps",
            access_token=business_token,
        )
        return data.get("success") is True

    async def register_phone_number(
        self,
        phone_number_id: str,
        business_token: str,
        pin: str,
    ) -> bool:
        """Register the customer's WhatsApp phone number for Cloud API."""

        self.ensure_configured()
        data = await self._request_json(
            method="POST",
            path=f"/{phone_number_id}/register",
            access_token=business_token,
            json_body={
                "messaging_product": "whatsapp",
                "pin": pin,
            },
        )
        return data.get("success") is True

    async def create_payment_link_utility_template(
        self,
        waba_id: str,
        business_token: str,
        template_name: str = WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME,
        language: str = WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE,
        category: str = WHATSAPP_PAYMENT_LINK_TEMPLATE_CATEGORY,
    ) -> MetaTemplateCreateResult:
        """Create the default customer-requested payment link Utility template."""

        components = [
            {
                "type": "BODY",
                "text": (
                    "Hi {{1}}, as requested, here is the payment link for "
                    "order {{2}}: {{3}}. This link is shared for the "
                    "transaction you requested."
                ),
                "example": {
                    "body_text": [
                        [
                            "Rahul",
                            "ORD-12345",
                            "https://example.com/pay/abc123",
                        ]
                    ]
                },
            }
        ]
        return await self.create_message_template(
            waba_id=waba_id,
            business_token=business_token,
            name=template_name,
            language=language,
            category=category,
            components=components,
        )

    async def create_message_template(
        self,
        waba_id: str,
        business_token: str,
        name: str,
        language: str,
        category: str,
        components: list[dict[str, Any]],
    ) -> MetaTemplateCreateResult:
        """Create a message template in the customer's WABA."""

        self.ensure_configured()
        data = await self._request_json(
            method="POST",
            path=f"/{waba_id}/message_templates",
            access_token=business_token,
            json_body={
                "name": name,
                "language": language,
                "category": category,
                "components": components,
            },
        )
        return MetaTemplateCreateResult(
            id=str(data["id"]) if data.get("id") is not None else None,
            status=str(data["status"]) if data.get("status") is not None else None,
            category=(
                str(data["category"]) if data.get("category") is not None else category
            ),
            raw_metadata=data,
        )

    async def send_payment_link_template_message(
        self,
        phone_number_id: str,
        business_token: str,
        recipient_phone: str,
        customer_name: str,
        order_reference: str,
        payment_link: str,
        template_name: str = WHATSAPP_PAYMENT_LINK_TEMPLATE_NAME,
        language: str = WHATSAPP_PAYMENT_LINK_TEMPLATE_LANGUAGE,
    ) -> MetaMessageSendResult:
        """Send the approved payment-link template through Cloud API."""

        data = await self.send_template_message(
            phone_number_id=phone_number_id,
            business_token=business_token,
            recipient_phone=recipient_phone,
            template_name=template_name,
            language=language,
            body_parameters=[
                customer_name,
                order_reference,
                payment_link,
            ],
        )
        return data

    async def send_template_message(
        self,
        phone_number_id: str,
        business_token: str,
        recipient_phone: str,
        template_name: str,
        language: str,
        body_parameters: list[str],
    ) -> MetaMessageSendResult:
        """Send a WhatsApp template message with text body parameters."""

        self.ensure_configured()
        data = await self._request_json(
            method="POST",
            path=f"/{phone_number_id}/messages",
            access_token=business_token,
            json_body={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_phone,
                "type": "template",
                "template": {
                    "name": template_name,
                    "language": {"code": language},
                    "components": [
                        {
                            "type": "body",
                            "parameters": [
                                {"type": "text", "text": parameter}
                                for parameter in body_parameters
                            ],
                        }
                    ],
                },
            },
        )
        message_id = None
        messages = data.get("messages")
        if isinstance(messages, list) and messages:
            first_message = messages[0]
            if isinstance(first_message, dict) and first_message.get("id") is not None:
                message_id = str(first_message["id"])
        return MetaMessageSendResult(message_id=message_id, raw_metadata=data)

    async def _request_json(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        access_token: Optional[str] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a Meta Graph request and normalize JSON errors."""

        path = path if path.startswith("/") else f"/{path}"
        if path == "/oauth/access_token":
            url = f"{self.graph_base_url}/{self.graph_api_version}{path}"
        else:
            url = f"{self.graph_base_url}/{self.graph_api_version}{path}"

        headers = {}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        async with create_http_client(timeout=self.timeout_seconds) as client:
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                )
            except httpx.HTTPError as e:
                raise MetaWhatsAppAPIError(f"Meta Graph request failed: {e}") from e

        try:
            data = response.json()
        except ValueError as e:
            raise MetaWhatsAppAPIError(
                "Meta Graph response was not valid JSON",
                status_code=response.status_code,
            ) from e

        if response.status_code >= 400:
            error = data.get("error") if isinstance(data, dict) else None
            if isinstance(error, dict):
                raise MetaWhatsAppAPIError(
                    str(error.get("message") or "Meta Graph request failed"),
                    status_code=response.status_code,
                    error_code=(
                        str(error.get("code"))
                        if error.get("code") is not None
                        else None
                    ),
                    error_subcode=(
                        str(error.get("error_subcode"))
                        if error.get("error_subcode") is not None
                        else None
                    ),
                )
            raise MetaWhatsAppAPIError(
                "Meta Graph request failed",
                status_code=response.status_code,
            )

        if not isinstance(data, dict):
            raise MetaWhatsAppAPIError(
                "Meta Graph response JSON was not an object",
                status_code=response.status_code,
            )
        return data
