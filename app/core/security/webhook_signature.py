"""
Shared telephony webhook authentication.

Every provider callback/answer/recording webhook must prove it came from the
real provider before the server acts on it. Twilio and Plivo sign their
requests (HMAC over the exact public URL + params); Exotel uses a shared secret
token on the query string. This module centralises verification so all webhook
routes share one correct, constant-time implementation.

Signature reconstruction uses ``APP_BASE_URL`` — the canonical public base the
providers were configured with — rather than ``request.url``, which behind a
load balancer reflects an internal host that differs from what the provider
actually signed.

Enforcement is on by default (secure). ``ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES``
exists only as an operational escape hatch; even when a provider's secret is
unset the verifier fails closed (rejects) rather than silently allowing.
"""

import hmac
from typing import Mapping, Optional

from fastapi import HTTPException, Request, status

from app.core.config.static import (
    APP_BASE_URL,
    ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES,
    EXOTEL_WEBHOOK_AUTH_TOKEN,
    PLIVO_AUTH_TOKEN,
    TWILIO_AUTH_TOKEN,
)
from app.core.logger import logger


def reconstruct_public_url(request: Request) -> str:
    """Rebuild the exact URL the provider signed (public base + path + query)."""
    if APP_BASE_URL:
        base = APP_BASE_URL.rstrip("/")
        url = f"{base}{request.url.path}"
    else:
        # No configured public base — best effort. Signature checks will only
        # pass in dev if the provider signed this exact host, which is fine.
        logger.warning(
            "APP_BASE_URL is unset; reconstructing webhook URL from request.url "
            "which may not match the signed URL behind a proxy"
        )
        url = str(request.url).split("?", 1)[0]
    query = request.url.query
    return f"{url}?{query}" if query else url


def verify_exotel_token(token: Optional[str]) -> bool:
    """Constant-time compare of the Exotel webhook token. Fails closed if unset."""
    if not EXOTEL_WEBHOOK_AUTH_TOKEN:
        return False
    return hmac.compare_digest(token or "", EXOTEL_WEBHOOK_AUTH_TOKEN)


def verify_twilio_signature(
    url: str, params: Mapping[str, str], signature: Optional[str]
) -> bool:
    """Verify an X-Twilio-Signature header. Fails closed if the token is unset."""
    if not TWILIO_AUTH_TOKEN or not signature:
        return False
    try:
        from twilio.request_validator import RequestValidator

        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        return bool(validator.validate(url, dict(params), signature))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Twilio signature validation error: {exc}")
        return False


def verify_plivo_signature(
    method: str, url: str, headers: Mapping[str, str], params: Mapping[str, str]
) -> bool:
    """Verify a Plivo V3 (preferred) or V2 signature. Fails closed if unset."""
    if not PLIVO_AUTH_TOKEN:
        return False
    try:
        import plivo.utils as plivo_utils

        v3_sig = headers.get("X-Plivo-Signature-V3")
        v3_nonce = headers.get("X-Plivo-Signature-V3-Nonce")
        if v3_sig and v3_nonce:
            return bool(
                plivo_utils.validate_v3_signature(
                    method.upper(),
                    url,
                    v3_nonce,
                    PLIVO_AUTH_TOKEN,
                    v3_sig,
                    dict(params) or None,
                )
            )
        v2_sig = headers.get("X-Plivo-Signature-V2")
        v2_nonce = headers.get("X-Plivo-Signature-V2-Nonce")
        if v2_sig and v2_nonce:
            return bool(
                plivo_utils.validate_signature(url, v2_nonce, v2_sig, PLIVO_AUTH_TOKEN)
            )
        return False
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(f"Plivo signature validation error: {exc}")
        return False


async def verify_provider_webhook(request: Request, provider: str) -> None:
    """Authenticate a provider webhook; raise HTTPException(401) on failure.

    Reads form data for POST requests (Starlette caches the body so callers may
    still read it again). Dispatches to the per-provider verifier.
    """
    if not ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES:
        logger.warning(
            f"Telephony webhook signature enforcement is DISABLED for {provider} "
            "(ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES=false) — do not run this in production"
        )
        return

    provider = provider.lower()
    url = reconstruct_public_url(request)

    if provider == "exotel":
        if not verify_exotel_token(request.query_params.get("auth_token")):
            _reject(provider)
        return

    params: dict[str, str] = {}
    if request.method == "POST":
        try:
            form = await request.form()
            params = {k: str(v) for k, v in form.items()}
        except Exception:
            params = {}

    if provider == "twilio":
        # Twilio signs the full URL (incl. query) for GET and URL+form for POST.
        sig = request.headers.get("X-Twilio-Signature")
        if not verify_twilio_signature(url, params, sig):
            _reject(provider)
        return

    if provider == "plivo":
        if not verify_plivo_signature(request.method, url, request.headers, params):
            _reject(provider)
        return

    # Unknown provider — fail closed.
    _reject(provider)


def _reject(provider: str) -> None:
    logger.warning(f"Rejected unauthenticated/forged {provider} webhook request")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Webhook signature verification failed",
    )
