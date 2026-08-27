import base64
import json
from typing import Any, Dict, Optional

import aiohttp

from app.core.config.static import (
    JUSPAY_BASE_URL,
    JUSPAY_TXNS_TIMEOUT_SECONDS,
)
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session


class JuspayError(RuntimeError):
    def __init__(self, message: str, status: Optional[int], body: Any):
        super().__init__(message)
        self.status = status
        self.body = body


def _form_fields(data: Dict[str, Any]) -> Dict[str, str]:
    """Flatten a payload for application/x-www-form-urlencoded.

    str(True) is "True", which Juspay rejects, and nested values travel as
    JSON strings inside a single field. None means "not sent".
    """
    fields: Dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if isinstance(value, bool):
            fields[key] = "true" if value else "false"
        elif isinstance(value, (dict, list)):
            fields[key] = json.dumps(value)
        else:
            fields[key] = str(value)
    return fields


async def request(
    method: str,
    path: str,
    *,
    api_key: Optional[str],
    merchant_id: Optional[str],
    base_url: Optional[str],
    json_body: Optional[Dict[str, Any]] = None,
    form_body: Optional[Dict[str, Any]] = None,
    routing_id: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    if not api_key:
        raise ValueError("api_key is required")
    if not merchant_id:
        raise ValueError("merchant_id is required")
    if json_body is None and form_body is None and method.upper() != "GET":
        raise ValueError("one of json_body or form_body is required")
    base_url = (base_url or JUSPAY_BASE_URL).rstrip("/")

    if api_key.startswith("Basic "):
        authorization = api_key
    else:
        authorization = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
    headers = {"Authorization": authorization}
    if extra_headers:
        headers.update(extra_headers)
    if routing_id:
        headers["x-routing-id"] = routing_id

    kwargs: Dict[str, Any] = {}
    if form_body is not None:
        kwargs["data"] = _form_fields(form_body)
    elif json_body is not None:
        kwargs["json"] = json_body

    logger.info(
        f">>> [juspay] {method} {base_url}{path} merchant_id={merchant_id} "
        f"api_key=***{api_key[-4:]} routing_id={routing_id} "
        f"body={kwargs.get('json') or kwargs.get('data')}"
    )
    async with create_aiohttp_session() as session:
        async with session.request(
            method,
            f"{base_url}{path}",
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=JUSPAY_TXNS_TIMEOUT_SECONDS),
            **kwargs,
        ) as response:
            text = await response.text()
            logger.info(
                f">>> [juspay] {method} {path} -> HTTP {response.status} body={text}"
            )
            try:
                decoded = json.loads(text)
            except ValueError:
                logger.error(
                    f">>> [juspay] {path} non-JSON body (HTTP {response.status})"
                )
                raise JuspayError(
                    f"Juspay {path} returned a non-JSON body (HTTP {response.status})",
                    response.status,
                    text,
                )
            if response.status >= 400:
                logger.error(
                    f">>> [juspay] {path} FAILED HTTP {response.status}: {decoded}"
                )
                raise JuspayError(
                    f"Juspay {path} failed with HTTP {response.status}",
                    response.status,
                    decoded,
                )
            return decoded
