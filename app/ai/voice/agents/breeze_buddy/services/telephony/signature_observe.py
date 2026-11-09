"""Observe-only signature checks for Plivo and Vobiz webhooks and media sockets.

Nothing is rejected here. Each request logs one ``telephony_signature`` line
(present / valid / replay) so the counts can be read before a route enforces.
Signature and nonce values are never logged.
"""

import base64
import hashlib
import hmac

from fastapi import Request, WebSocket
from plivo.utils import validate_v3_signature

from app.core.config.static import (
    APP_BASE_URL,
    PLIVO_AUTH_TOKEN,
    VOBIZ_AUTH_TOKEN,
    VOICE_AGENT_POD_NAME,
)
from app.core.logger import logger
from app.services.redis import get_redis_service

NONCE_TTL_SECONDS = 24 * 60 * 60


def vobiz_signature_ok(url: str, nonce: str, signature: str) -> bool:
    """Vobiz V3: base64(HMAC-SHA256(auth token, URL without query + "." + nonce))."""
    text = f"{url}.{nonce}"
    digest = hmac.new(VOBIZ_AUTH_TOKEN.encode(), text.encode(), hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


async def nonce_replay(provider: str, nonce: str) -> str:
    """ "false" the first time a nonce is seen, "true" after that, "error" if Redis fails."""
    try:
        client = await (await get_redis_service()).get_client()
        first = await client.set(
            f"telephony_nonce:{provider}:{nonce}", "1", nx=True, ex=NONCE_TTL_SECONDS
        )
    except Exception as e:
        logger.warning(f"telephony_signature nonce check failed: {type(e).__name__}")
        return "error"
    return "false" if first else "true"


async def observe_webhook_signature(
    request: Request, provider: str, route: str
) -> None:
    """Log whether a Plivo or Vobiz webhook carries a valid V3 signature."""
    if provider not in ("plivo", "vobiz"):
        return
    try:
        base_url = APP_BASE_URL.rstrip("/") + request.url.path
        form = await request.form()
        if provider == "plivo":
            token = PLIVO_AUTH_TOKEN
            signature = request.headers.get("X-Plivo-Signature-V3", "")
            nonce = request.headers.get("X-Plivo-Signature-V3-Nonce", "")
        else:
            token = VOBIZ_AUTH_TOKEN
            signature = request.headers.get("X-Vobiz-Signature-V3", "")
            nonce = request.headers.get("X-Vobiz-Signature-V3-Nonce", "")

        present = bool(signature and nonce)
        valid = False
        if present and token and provider == "plivo":
            url = base_url
            if request.url.query:
                url += f"?{request.url.query}"
            params = {k: v for k, v in form.items() if isinstance(v, str)}
            valid = bool(
                validate_v3_signature(
                    request.method, url, nonce, token, signature, params or None
                )
            )
        if present and token and provider == "vobiz":
            valid = vobiz_signature_ok(base_url, nonce, signature)

        replay = await nonce_replay(provider, nonce) if valid else "-"
        call = form.get("CallUUID") or request.query_params.get("CallUUID") or "-"
        logger.info(
            f"telephony_signature provider={provider} route={route} "
            f"method={request.method} present={str(present).lower()} "
            f"valid={str(valid).lower()} replay={replay} "
            f"token={'set' if token else 'unset'} call={call}"
        )
    except Exception as e:
        logger.warning(
            f"telephony_signature provider={provider} route={route} "
            f"error={type(e).__name__}"
        )


async def observe_websocket_signature(websocket: WebSocket, provider: str) -> None:
    """Log whether a media socket upgrade carries a valid V3 signature.

    Plivo signs http://<host><path> with no query, using the stream URL from the
    answer XML. When Smart Router allocated the pod, that path starts with
    /ws/pod/<pod>, which the ingress strips, so the path with this pod's own
    POD_NAME put back is tried as well. Vobiz does not sign the socket; its line
    records that no signature came.
    """
    provider = provider.lower()
    if provider not in ("plivo", "vobiz"):
        return
    pod = VOICE_AGENT_POD_NAME
    try:
        prefix = "X-Plivo" if provider == "plivo" else "X-Vobiz"
        signature = websocket.headers.get(f"{prefix}-Signature-V3", "")
        nonce = websocket.headers.get(f"{prefix}-Signature-V3-Nonce", "")
        present = bool(signature and nonce)

        matched = "none"
        if present and provider == "plivo" and PLIVO_AUTH_TOKEN:
            host = websocket.headers.get("host", "")
            path = websocket.url.path
            urls = {"direct": f"http://{host}{path}"}
            if pod:
                urls["pod"] = f"http://{host}/ws/pod/{pod}{path}"
            for name, url in urls.items():
                if validate_v3_signature(
                    "GET", url, nonce, PLIVO_AUTH_TOKEN, signature
                ):
                    matched = name
                    break

        valid = matched != "none"
        replay = await nonce_replay(provider, nonce) if valid else "-"
        logger.info(
            f"telephony_signature provider={provider} route=ws method=GET "
            f"present={str(present).lower()} valid={str(valid).lower()} "
            f"matched={matched} replay={replay} pod={pod or '-'}"
        )
    except Exception as e:
        logger.warning(
            f"telephony_signature provider={provider} route=ws "
            f"error={type(e).__name__}"
        )
