"""Client for sending merchant-scoped Meta WhatsApp messages."""

import asyncio
import re
from typing import Any, Optional

import aiohttp

from app.core.config.static import (
    META_WHATSAPP_GRAPH_API_BASE_URL,
    META_WHATSAPP_GRAPH_API_VERSION,
)
from app.core.logger import logger


class MetaWhatsAppService:
    """Send template or text messages through the Meta Graph API."""

    def __init__(
        self,
        *,
        base_url: str = META_WHATSAPP_GRAPH_API_BASE_URL,
        api_version: str = META_WHATSAPP_GRAPH_API_VERSION,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_version = api_version.strip("/")

    @staticmethod
    def normalize_recipient(phone_number: str) -> Optional[str]:
        """Return an E.164-style recipient without its leading plus sign."""
        normalized = re.sub(r"[\s()\-]", "", phone_number or "")
        if normalized.startswith("+"):
            normalized = normalized[1:]
        if not normalized.isdigit() or not 8 <= len(normalized) <= 15:
            return None
        return normalized

    async def send_message(
        self,
        *,
        session: Optional[aiohttp.ClientSession],
        access_token: str,
        phone_number_id: str,
        recipient_phone_number: str,
        template_name: Optional[str] = None,
        values: Optional[list[str]] = None,
        message: Optional[str] = None,
        language_code: str = "en_US",
    ) -> dict[str, Any]:
        """Send an approved template or a free-form text message."""
        recipient = self.normalize_recipient(recipient_phone_number)
        if not recipient:
            return {"success": False, "error": "Invalid recipient phone number"}
        if not phone_number_id or not access_token:
            return {"success": False, "error": "WhatsApp connector is incomplete"}

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
        }
        if template_name:
            components: list[dict[str, Any]] = []
            if values:
                components.append(
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": value} for value in values
                        ],
                    }
                )
            payload.update(
                {
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"policy": "deterministic", "code": language_code},
                        "components": components,
                    },
                }
            )
        elif message:
            payload.update({"type": "text", "text": {"body": message}})
        else:
            return {
                "success": False,
                "error": "message is required when template is not provided",
            }

        url = f"{self._base_url}/{self._api_version}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        recipient_suffix = recipient[-4:]
        try:
            if session is not None and not session.closed:
                return await self._post(session, url, headers, payload, recipient_suffix)

            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as owned_session:
                return await self._post(
                    owned_session, url, headers, payload, recipient_suffix
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.error(f"Meta WhatsApp request failed for recipient ***{recipient_suffix}: {e}")
            return {"success": False, "error": "WhatsApp provider request failed"}

    @staticmethod
    async def _post(
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        recipient_suffix: str,
    ) -> dict[str, Any]:
        async with session.post(url, json=payload, headers=headers) as response:
            try:
                body = await response.json(content_type=None)
            except Exception:
                body = {}

            if 200 <= response.status < 300:
                messages = body.get("messages", []) if isinstance(body, dict) else []
                message_id = messages[0].get("id") if messages else None
                logger.info(
                    "Meta WhatsApp message accepted for "
                    f"recipient ***{recipient_suffix}, message_id={message_id}"
                )
                return {"success": True, "message_id": message_id}

            error = body.get("error", {}) if isinstance(body, dict) else {}
            error_message = error.get("message") or "WhatsApp provider rejected message"
            error_code = error.get("code")
            logger.warning(
                "Meta WhatsApp rejected message for "
                f"recipient ***{recipient_suffix}: status={response.status}, code={error_code}"
            )
            return {
                "success": False,
                "error": error_message,
                "error_code": error_code,
                "status_code": response.status,
            }
