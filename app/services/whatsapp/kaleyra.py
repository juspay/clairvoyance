"""
Kaleyra WhatsApp Service

Provides functionality to send WhatsApp messages via Kaleyra V2 API.
Based on Vayu's implementation pattern.

API Reference: https://docs.kaleyra.com/
Endpoint: POST https://api.in.kaleyra.io/v2/{SID}/whatsapp/{sender_phone}/messages
"""

from typing import Any, Optional

import aiohttp

from app.core.config.static import (
    KALEYRA_API_BASE_URL,
    KALEYRA_API_KEY,
    KALEYRA_SID,
    KALEYRA_WHATSAPP_FROM,
    KALEYRA_WHATSAPP_TEMPLATE,
)
from app.core.logger import logger


class KaleyraWhatsAppService:
    """
    Kaleyra V2 WhatsApp API client.

    Sends WhatsApp template messages using Kaleyra's V2 API.
    Credentials are loaded from environment variables.
    """

    def __init__(self) -> None:
        """Initialize the Kaleyra WhatsApp service with env-based config."""
        self.base_url = KALEYRA_API_BASE_URL.rstrip("/")
        self.sid = KALEYRA_SID
        self.api_key = KALEYRA_API_KEY
        self.from_number = KALEYRA_WHATSAPP_FROM
        self.default_template = KALEYRA_WHATSAPP_TEMPLATE

    def _is_configured(self) -> bool:
        """Check if all required Kaleyra credentials are configured."""
        return bool(self.sid and self.api_key and self.from_number)

    def _ensure_country_code(self, phone: str) -> str:
        """
        Ensure phone number has country code prefix.

        Args:
            phone: Phone number (may or may not have + prefix)

        Returns:
            Phone number with + prefix
        """
        phone = phone.strip()
        if not phone.startswith("+"):
            # Assume Indian number if no country code
            if phone.startswith("91"):
                return f"+{phone}"
            return f"+91{phone}"
        return phone

    async def send_template_message(
        self,
        to: str,
        template_name: Optional[str] = None,
        template_params: Optional[list[str]] = None,
        language_code: str = "en",
    ) -> dict[str, Any]:
        """
        Send a WhatsApp template message via Kaleyra V2 API.

        Args:
            to: Recipient phone number (with or without country code)
            template_name: Pre-approved WhatsApp template name.
                          Falls back to KALEYRA_WHATSAPP_TEMPLATE env var.
            template_params: List of text values for template body parameters.
                            Order must match template placeholders ({{1}}, {{2}}, etc.)
            language_code: Template language code (default: "en")

        Returns:
            Dict with keys:
                - success: bool indicating if message was sent
                - data: API response data (on success)
                - error: Error message (on failure)
                - message_id: Kaleyra message ID (on success, if available)
        """
        if not self._is_configured():
            logger.warning(
                "[KaleyraWhatsApp] Service not configured - missing credentials"
            )
            return {
                "success": False,
                "error": "Kaleyra WhatsApp service not configured",
            }

        # Use default template if not specified
        template = template_name or self.default_template
        if not template:
            logger.error("[KaleyraWhatsApp] No template name provided or configured")
            return {
                "success": False,
                "error": "No WhatsApp template name configured",
            }

        # Ensure phone has country code
        recipient = self._ensure_country_code(to)
        sender = self._ensure_country_code(self.from_number)

        # Build Kaleyra V2 API URL
        # Format: POST /{SID}/whatsapp/{sender_phone}/messages
        url = f"{self.base_url}/{self.sid}/whatsapp/{sender}/messages"

        # Build request headers
        headers = {
            "Content-Type": "application/json",
            "api-key": self.api_key,
            "cache-control": "no-cache",
        }

        # Build template components
        components: list[dict[str, Any]] = []
        if template_params:
            components.append(
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": param} for param in template_params
                    ],
                }
            )

        # Build Kaleyra V2 payload
        payload = {
            "messaging_object": {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient,
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": language_code},
                    "components": components,
                },
            }
        }

        logger.info(
            f"[KaleyraWhatsApp] Sending template '{template}' to {recipient} "
            f"with {len(template_params or [])} params"
        )

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as response:
                    response_data = await response.json()

                    if 200 <= response.status < 300:
                        logger.info(
                            f"[KaleyraWhatsApp] Message sent successfully to {recipient}"
                        )
                        return {
                            "success": True,
                            "data": response_data,
                            "message_id": response_data.get("id"),
                        }
                    else:
                        error_msg = response_data.get(
                            "error", response_data.get("message", "Unknown error")
                        )
                        logger.error(
                            f"[KaleyraWhatsApp] API error: {response.status} - {error_msg}"
                        )
                        return {
                            "success": False,
                            "error": error_msg,
                            "status_code": response.status,
                            "data": response_data,
                        }

        except aiohttp.ClientError as e:
            logger.error(f"[KaleyraWhatsApp] Network error: {e}")
            return {
                "success": False,
                "error": f"Network error: {str(e)}",
            }
        except Exception as e:
            logger.error(f"[KaleyraWhatsApp] Unexpected error: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Unexpected error: {str(e)}",
            }


# Global singleton instance
kaleyra_whatsapp = KaleyraWhatsAppService()
