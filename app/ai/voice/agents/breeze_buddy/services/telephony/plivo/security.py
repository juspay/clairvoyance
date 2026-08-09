"""Plivo webhook and media WebSocket signature checks (PT-02/05/12/23)."""

from fastapi import HTTPException, Request, WebSocket
from plivo.utils import validate_v3_signature

from app.core.config.static import APP_BASE_URL, PLIVO_AUTH_TOKEN
from app.core.logger import logger


async def verify_plivo_webhook(request: Request) -> None:
    """401 unless a Plivo webhook carries a valid X-Plivo-Signature-V3.

    V2 is not accepted: it signs only the URL path, so a replayed V2 header
    would authenticate any body. The URL is rebuilt from APP_BASE_URL because
    behind a load balancer request.url is an internal host.
    """
    url = APP_BASE_URL.rstrip("/") + request.url.path
    if request.url.query:
        url += f"?{request.url.query}"
    params = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
    sig = request.headers.get("X-Plivo-Signature-V3")
    nonce = request.headers.get("X-Plivo-Signature-V3-Nonce")
    try:
        ok = bool(
            sig
            and nonce
            and PLIVO_AUTH_TOKEN
            and validate_v3_signature(
                request.method, url, nonce, PLIVO_AUTH_TOKEN, sig, params or None
            )
        )
    except Exception:
        ok = False
    if not ok:
        logger.warning(f"Rejected unsigned/forged plivo webhook: {request.url.path}")
        raise HTTPException(
            status_code=401, detail="Webhook signature verification failed"
        )


def plivo_websocket_signature_ok(websocket: WebSocket) -> bool:
    """True if a media WebSocket upgrade carries a valid X-Plivo-Signature-V3.

    Plivo signs the upgrade as http://<host><path> with no query string. The
    host comes from the Host header because Smart Router may hand Plivo a
    pod-specific URL; a forged host simply fails the signature.
    """
    sig = websocket.headers.get("X-Plivo-Signature-V3")
    nonce = websocket.headers.get("X-Plivo-Signature-V3-Nonce")
    url = f"http://{websocket.headers.get('host', '')}{websocket.url.path}"
    try:
        return bool(
            sig
            and nonce
            and PLIVO_AUTH_TOKEN
            and validate_v3_signature("GET", url, nonce, PLIVO_AUTH_TOKEN, sig)
        )
    except Exception:
        return False
