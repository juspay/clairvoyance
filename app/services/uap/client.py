import base64
import json
from typing import Any, Dict, Optional

import aiohttp

from app.core.config.static import (
    JUSPAY_BASE_URL,
    JUSPAY_TXNS_TIMEOUT_SECONDS,
)
from app.core.transport.http_client import create_aiohttp_session


class JuspayError(RuntimeError):
    def __init__(self, message: str, status: Optional[int], body: Any):
        super().__init__(message)
        self.status = status
        self.body = body


async def request(
    method: str,
    path: str,
    *,
    api_key: Optional[str],
    merchant_id: Optional[str],
    base_url: Optional[str],
    json_body: Dict[str, Any],
) -> Dict[str, Any]:
    if not api_key:
        raise ValueError("api_key is required")
    if not merchant_id:
        raise ValueError("merchant_id is required")
    base_url = (base_url or JUSPAY_BASE_URL).rstrip("/")

    token = base64.b64encode(f"{api_key}:".encode()).decode()
    headers = {"Authorization": f"Basic {token}", "x-merchantid": merchant_id}
    async with create_aiohttp_session() as session:
        async with session.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=JUSPAY_TXNS_TIMEOUT_SECONDS),
        ) as response:
            text = await response.text()
            try:
                decoded = json.loads(text)
            except ValueError:
                raise JuspayError(
                    f"Juspay {path} returned a non-JSON body (HTTP {response.status})",
                    response.status,
                    text,
                )
            if response.status >= 400:
                raise JuspayError(
                    f"Juspay {path} failed with HTTP {response.status}",
                    response.status,
                    decoded,
                )
            return decoded
