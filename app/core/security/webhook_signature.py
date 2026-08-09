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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import HTTPException, Request, status

from app.core.config.static import (
    APP_BASE_URL,
    ENFORCE_TELEPHONY_WEBHOOK_SIGNATURES,
    EXOTEL_WEBHOOK_AUTH_TOKEN,
    PLIVO_AUTH_TOKEN,
    TELEPHONY_WEBHOOK_PATH_PREFIX,
    TWILIO_AUTH_TOKEN,
)
from app.core.logger import logger

REDACTED = "REDACTED"


def redact_query_param(url: str, param: str = "auth_token") -> str:
    """Return ``url`` with ``param``'s value replaced by ``REDACTED``.

    The Exotel status-callback URL carries the shared webhook secret as a query
    parameter — that is the only channel Exotel offers — so the URL itself is
    secret-bearing and must never reach a log sink. Use this anywhere such a URL
    is logged. Returns the input unchanged if it cannot be parsed, since a
    logging helper must not raise.
    """
    try:
        parts = urlsplit(url)
        if not parts.query:
            return url
        pairs = [
            (k, REDACTED if k == param else v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment)
        )
    except Exception:  # pragma: no cover
        return url


def reconstruct_public_url(request: Request) -> str:
    """Rebuild the exact URL the provider signed (public base + path + query).

    ``TELEPHONY_WEBHOOK_PATH_PREFIX`` covers the ingress-rewrite case: providers
    sign the externally-visible URL, so when a proxy strips a prefix before the
    request reaches us, ``request.url.path`` is shorter than what was signed and
    every legitimate webhook would be rejected. The prefix comes from config
    rather than ``X-Forwarded-Prefix`` on purpose — see static.py.
    """
    if APP_BASE_URL:
        base = APP_BASE_URL.rstrip("/")
        url = f"{base}{TELEPHONY_WEBHOOK_PATH_PREFIX}{request.url.path}"
    else:
        logger.warning(
            "APP_BASE_URL is unset; reconstructing webhook URL from request.url "
            "which may not match the signed URL behind a proxy"
        )
        url = str(request.url).split("?", 1)[0]
    query = request.url.query
    return f"{url}?{query}" if query else url


def verify_exotel_token(token: Optional[str]) -> bool:
    """Constant-time compare of the Exotel webhook token. Fails closed if unset.

    Both operands are encoded to UTF-8 first: ``hmac.compare_digest`` raises
    ``TypeError`` on ``str`` containing non-ASCII, and the token is caller-
    supplied, so comparing strings would let anyone turn a controlled 401 into
    an unhandled 500 by sending a non-ASCII ``auth_token``.
    """
    if not EXOTEL_WEBHOOK_AUTH_TOKEN:
        return False
    return hmac.compare_digest(
        (token or "").encode("utf-8"), EXOTEL_WEBHOOK_AUTH_TOKEN.encode("utf-8")
    )


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
    except Exception as exc:  # pragma: no cover
        logger.warning(f"Twilio signature validation error: {exc}")
        return False


def verify_plivo_signature(
    method: str, url: str, headers: Mapping[str, str], params: Mapping[str, str]
) -> bool:
    """Verify a Plivo V3 signature. Fails closed if unset.

    V2 is deliberately not accepted. It signs only the URL path and the nonce,
    never the body or query, so a replayed V2 header would authenticate a forged
    body. Plivo sends V3 on every voice request, so nothing legitimate needs V2.
    """
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
        return False
    except Exception as exc:  # pragma: no cover
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

    missing = ""
    if provider == "exotel" and not EXOTEL_WEBHOOK_AUTH_TOKEN:
        missing = "EXOTEL_WEBHOOK_AUTH_TOKEN"
    elif provider == "twilio" and not TWILIO_AUTH_TOKEN:
        missing = "TWILIO_AUTH_TOKEN"
    elif provider == "plivo" and not PLIVO_AUTH_TOKEN:
        missing = "PLIVO_AUTH_TOKEN"
    if missing:
        logger.error(f"{missing} is not configured; rejecting {provider} webhook")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Webhook signature verification failed",
        )

    url = reconstruct_public_url(request)

    if provider == "exotel":
        if not verify_exotel_token(request.query_params.get("auth_token")):
            _reject(provider)
        return

    params: dict[str, str] = {}
    if request.method == "POST":
        try:
            form = await request.form()
            params = {k: v for k, v in form.items() if isinstance(v, str)}
            dropped = len(form) - len(params)
            if dropped:
                logger.warning(
                    f"{provider} webhook carried {dropped} non-text form field(s); "
                    "ignored for signature verification (providers sign urlencoded "
                    "bodies only)"
                )
        except Exception:
            params = {}

    if provider == "twilio":
        sig = request.headers.get("X-Twilio-Signature")
        if not verify_twilio_signature(url, params, sig):
            _reject(provider)
        return

    if provider == "plivo":
        if not verify_plivo_signature(request.method, url, request.headers, params):
            _reject(provider)
        return

    _reject(provider)


def _reject(provider: str) -> None:
    logger.warning(f"Rejected unauthenticated/forged {provider} webhook request")
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Webhook signature verification failed",
    )
