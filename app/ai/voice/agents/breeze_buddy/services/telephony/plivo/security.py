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
        # The query is left out of the log: it can carry phone numbers.
        logger.warning(
            f"Rejected unsigned/forged plivo webhook: expected a Plivo signature "
            f"for {APP_BASE_URL.rstrip('/')}{request.url.path}; got "
            f"signature={'present' if sig else 'missing'}, "
            f"nonce={'present' if nonce else 'missing'}"
            + ("" if PLIVO_AUTH_TOKEN else ", PLIVO_AUTH_TOKEN unset")
        )
        raise HTTPException(
            status_code=401, detail="Webhook signature verification failed"
        )


def plivo_websocket_signature_ok(websocket: WebSocket) -> bool:
    """True if a media WebSocket upgrade carries a valid X-Plivo-Signature-V3.

    Plivo signs the upgrade as http://<host><path> with no query string, using
    the stream URL it was given in the answer XML. When Smart Router allocated
    a pod, that path starts with /ws/pod/<pod>, which nginx strips before the
    pod sees the request; nginx sends the original request URI in
    X-Original-URI, so the path is read from there. Without the header the
    path is the one received: a direct stream, with no Smart Router.

    The header only chooses which URL is checked; the signature must still
    match it.
    """
    if not PLIVO_AUTH_TOKEN:
        logger.warning("Plivo media websocket signature: PLIVO_AUTH_TOKEN is unset")
        return False
    sig = websocket.headers.get("X-Plivo-Signature-V3")
    nonce = websocket.headers.get("X-Plivo-Signature-V3-Nonce")
    if not (sig and nonce):
        logger.warning(
            "Plivo media websocket signature: expected X-Plivo-Signature-V3 and "
            f"its nonce; got signature={'present' if sig else 'missing'}, "
            f"nonce={'present' if nonce else 'missing'}"
        )
        return False
    # Queries are left out of the URL and the log: they carry phone numbers.
    original_uri = websocket.headers.get("X-Original-URI")
    original_path = original_uri.split("?", 1)[0] if original_uri else None
    url = f"http://{websocket.headers.get('host', '')}{original_path or websocket.url.path}"
    try:
        ok = validate_v3_signature("GET", url, nonce, PLIVO_AUTH_TOKEN, sig)
    except Exception:
        ok = False
    if not ok:
        logger.warning(
            f"Plivo media websocket signature: expected a Plivo signature for {url}; "
            f"got path={websocket.url.path}, X-Original-URI={original_path or 'none'}"
        )
    return ok
