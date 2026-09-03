"""Shared widget-public utilities used by both the chat and voice widget
routers (CHAT_MODE.md §14).

Three concerns live here so the chat (``chat/widget.py``) and voice
(``daily_widget/handlers.py``) sub-routers stay focused on their own
flow:

1. **Best-effort caller IP** for rate-limit keying. Honours
   ``X-Forwarded-For`` (first hop) when present so a load balancer
   doesn't make every visitor share one bucket.
2. **Origin / Referer extraction + per-merchant allowlist check**.
   Empty ``allowed_origins`` is deny-all (safer default than allow-all).
3. **One-call ``resolve_widget_config_for_request``** that loads the
   row, enforces active + origin, and applies the per-IP rate limit
   before returning the config to the handler.

CORS preflight is a separate concern — see ``options_cors_response``
below. The browser's preflight must succeed for *any* origin so the
request can reach our handler; only then can the per-merchant origin
allowlist run. The two layers (CORS = transport, allowlist =
application) are intentionally split.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from fastapi import HTTPException, Request, Response, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.widget_config import (
    get_widget_config_by_public_key,
)
from app.schemas.breeze_buddy.widget_config import WidgetConfigResponse
from app.services.redis.rate_limit import check_rate_limit

# One-hour fixed window for all widget IP buckets — the cap (per
# session-creates, per messages, per voice-connects) is what shapes
# abuse resistance, not the granularity. Matches demo (``chat/demo.py``).
_RATE_WINDOW_SECONDS = 3600
_RATE_LIMIT_PREFIX = "widget"

# The Origin header is spoofable from a non-browser client and the
# public_widget_key is public, so per-IP limits alone don't bound a merchant's
# aggregate spend once an attacker rotates source IPs (PT-18). This multiplier
# turns each per-IP cap into an additional key-wide (cross-IP) cap, so no single
# action can be amplified past `multiplier x per-IP limit` no matter how many
# IPs are used. Each action keeps its own counter (see
# _enforce_widget_aggregate_limit), so the ceiling on a key is the sum of its
# per-action caps rather than one shared budget.
_WIDGET_AGGREGATE_MULTIPLIER = 20


def client_ip(request: Request) -> str:
    """Best-effort caller IP for rate-limit keying.

    Honours ``X-Forwarded-For`` *last* hop when present — the last entry
    is the one our trusted proxy / load balancer wrote, every earlier hop
    is attacker-controllable. Falls back to ``request.client.host`` (the
    direct peer). Returns ``unknown`` when both are missing — that's one
    shared bucket, which is the safest default.

    Earlier implementations took the first XFF hop, which let any client
    iterate the header on each request and get unbounded rate-limit
    buckets — defeating the cap. Last-hop assumes the deployment is
    behind a known proxy that strips client-supplied XFF. If you ever
    deploy widget endpoints WITHOUT a trusted proxy in front, set the
    proxy header explicitly (``X-Real-IP``) and switch to read that.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        # rsplit picks the *last* IP — the one written by our trusted
        # last-hop. ``unknown`` if the header is malformed / empty.
        last = forwarded.rsplit(",", 1)[-1].strip()
        if last:
            return last
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _origin_from_referer(referer: Optional[str]) -> Optional[str]:
    """Strip a Referer URL down to ``scheme://host[:port]`` for allowlist
    comparison. Returns None when the header is missing or unparseable.
    """
    if not referer:
        return None
    try:
        parsed = urlparse(referer)
    except Exception:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _caller_origin(request: Request) -> Optional[str]:
    """Pick the caller's origin: ``Origin`` header wins; ``Referer`` is a
    fallback for the rare cases where a browser strips Origin (legacy
    cross-origin GETs, some privacy proxies).
    """
    origin = request.headers.get("origin")
    if origin:
        return origin.strip() or None
    return _origin_from_referer(request.headers.get("referer"))


async def _enforce_widget_ip_limit(
    *, request: Request, bucket: str, limit: int, widget_config_id: str
) -> None:
    """Raise 429 with ``Retry-After`` when ``request``'s IP is over ``limit``.

    Mirrors the demo helper in ``chat/demo.py`` but namespaces the Redis
    keys under ``"widget"`` so demo and widget counters don't share
    buckets.

    The Redis identifier is ``"{widget_config_id}:{client_ip}"`` so each
    merchant has its OWN per-IP bucket — without this prefix, traffic to
    one merchant's widget would burn another merchant's quota.
    Combined with the last-hop XFF parsing in ``client_ip``, an attacker
    rotating XFF values can no longer escape the cap (last hop is
    proxy-written, not client-controllable).

    ``fail_closed=True`` is passed through to the rate limiter so a
    Redis outage denies new widget traffic instead of opening the
    floodgates — widget endpoints are anonymous + LLM-cost-amplifying;
    fail-open here is genuinely worse than a brief 503.
    """
    decision = await check_rate_limit(
        bucket=bucket,
        identifier=f"{widget_config_id}:{client_ip(request)}",
        limit=limit,
        window_seconds=_RATE_WINDOW_SECONDS,
        prefix=_RATE_LIMIT_PREFIX,
        fail_closed=True,
    )
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Widget rate limit hit ({decision.count}/{decision.limit} per hour). "
            "Try again later."
        ),
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


async def _enforce_widget_aggregate_limit(
    *, bucket: str, limit: int, widget_config_id: str
) -> None:
    """Raise 429 when one public_widget_key exceeds this action's cross-IP cap.

    Keyed on ``widget_config_id`` ONLY (no client IP), so rotating source IPs
    cannot escape it (PT-18). Fail-closed on Redis outage, like the per-IP cap.

    Note the cap is *per action type*, not one shared budget: the bucket name is
    part of the key, so ``message`` and ``transcribe`` each carry their own
    cross-IP ceiling. That is deliberate — the per-action limits differ by an
    order of magnitude, and collapsing them into one counter would let cheap
    calls exhaust the budget for expensive ones. It does mean total spend on a
    key is bounded by the *sum* of the per-action caps, not by any single one.
    """
    decision = await check_rate_limit(
        bucket=f"{bucket}:agg",
        identifier=widget_config_id,
        limit=limit,
        window_seconds=_RATE_WINDOW_SECONDS,
        prefix=_RATE_LIMIT_PREFIX,
        fail_closed=True,
    )
    if decision.allowed:
        return
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=(
            f"Widget usage limit hit ({decision.count}/{decision.limit} per hour "
            "for this widget). Try again later."
        ),
        headers={"Retry-After": str(decision.retry_after_seconds)},
    )


async def resolve_widget_config_for_request(
    *,
    request: Request,
    public_widget_key: str,
    rate_bucket: str,
    rate_limit: Optional[int] = None,
) -> WidgetConfigResponse:
    """Load the widget_config + enforce origin + per-IP rate limit.

    Args:
        request: The FastAPI request (used for headers + client IP).
        public_widget_key: Opaque key the embed sent in its JSON body.
        rate_bucket: Logical bucket name (e.g. ``"chat_session"``,
            ``"chat_message"``, ``"voice_connect"``). Lets multiple
            widget routes share Redis storage without colliding.
        rate_limit: Override the default cap pulled from the row. If
            ``None``, the route is expected to pick a value off the
            loaded ``WidgetConfigResponse`` itself — used when the row
            hadn't been loaded yet.

    Behaviour:
        - Returns 404 (no echo) when the key is unknown or the row is
          inactive — same reconnaissance-hardening as the demo router.
        - Returns 403 when the caller's origin (or Referer-derived
          origin) isn't in ``allowed_origins`` — same status when
          ``allowed_origins`` is empty (deny-all by default).
        - Returns 429 with ``Retry-After`` when the per-IP bucket for
          ``rate_bucket`` is over ``rate_limit``.
    """
    cfg = await get_widget_config_by_public_key(public_widget_key)
    if cfg is None or not cfg.active:
        # Don't differentiate "unknown" from "inactive" to the caller —
        # both are 404 with no detail. The log carries the prefix for
        # operators debugging a misconfigured embed.
        logger.warning(
            "widget: unknown or inactive public_widget_key "
            f"(prefix={public_widget_key[:6]!r})"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget configuration not found",
        )

    origin = _caller_origin(request)
    if origin is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin required for widget routes",
        )
    if not cfg.allowed_origins or origin not in cfg.allowed_origins:
        logger.warning(
            f"widget: origin {origin!r} not in allowed_origins for "
            f"widget_config={cfg.id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Origin not permitted for this widget",
        )

    effective_limit = (
        rate_limit if rate_limit is not None else cfg.max_sessions_per_ip_hour
    )
    await _enforce_widget_ip_limit(
        request=request,
        bucket=rate_bucket,
        limit=effective_limit,
        widget_config_id=cfg.id,
    )
    # Cross-IP aggregate cap so IP rotation can't run up unbounded spend on this
    # merchant's public_widget_key (PT-18).
    await _enforce_widget_aggregate_limit(
        bucket=rate_bucket,
        limit=effective_limit * _WIDGET_AGGREGATE_MULTIPLIER,
        widget_config_id=cfg.id,
    )
    return cfg


async def enforce_widget_ip_limit(
    *, request: Request, bucket: str, limit: int, widget_config_id: str
) -> None:
    """Public alias for the IP rate-limit helper.

    Used by routes that already resolved a ``widget_config`` from a
    session-bound JWT (no key lookup needed) but still want to bound
    follow-up calls — e.g., ``POST /chat/widget/session/{id}/message``
    enforcing ``max_messages_per_ip_hour``. ``widget_config_id`` is
    required so the per-IP bucket is also scoped per-merchant (one
    merchant's traffic can't exhaust another's quota).
    """
    await _enforce_widget_ip_limit(
        request=request,
        bucket=bucket,
        limit=limit,
        widget_config_id=widget_config_id,
    )
    # Cross-IP aggregate cap on this follow-up action too (PT-18).
    await _enforce_widget_aggregate_limit(
        bucket=bucket,
        limit=limit * _WIDGET_AGGREGATE_MULTIPLIER,
        widget_config_id=widget_config_id,
    )


# ---------------------------------------------------------------------------
# CORS preflight
# ---------------------------------------------------------------------------

# Permissive CORS headers for widget routes. The browser must be able to
# issue the preflight from any merchant origin so the request reaches our
# handler — the *application-layer* allowlist (``allowed_origins`` above)
# is what gates production traffic. Credentials are intentionally not
# enabled: widget tokens travel in ``Authorization``, and
# ``Access-Control-Allow-Credentials: true`` combined with ``Origin: *``
# is invalid per spec.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    # GET is needed for the session-resume endpoint
    # (`GET /widget/session/{id}`). Browser-side preflight blocks any
    # method not listed here when the request carries Authorization.
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "authorization, content-type",
    "Access-Control-Max-Age": "600",
}


def options_cors_response() -> Response:
    """204 response with permissive CORS headers for OPTIONS preflight.

    Mount an OPTIONS handler on each widget router that returns this so
    the browser preflight succeeds for any origin. The actual per-
    merchant allowlist runs inside the corresponding POST handler.
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_CORS_HEADERS)


__all__ = [
    "client_ip",
    "enforce_widget_ip_limit",
    "options_cors_response",
    "resolve_widget_config_for_request",
]
