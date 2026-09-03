"""Public storefront-config resolve for the Assist widget.

The theme-extension loader on a merchant's storefront calls this on page
view to learn whether the widget is enabled for the merchant and how to dress
it. It is the only widget endpoint keyed by the MERCHANT'S STOREFRONT
DOMAIN rather than by ``public_widget_key`` — the loader knows nothing
but the domain it runs on; the key is part of this endpoint's ANSWER,
not its input.

Trust model mirrors ``POST /widget/session``: anonymous, but the caller's
Origin must be in the row's ``allowed_origins`` and per-IP rate limits
apply. Unknown merchant, inactive row and origin mismatch are indistinguishable
to the caller (404/403 with no detail) so the door isn't enumerable.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.ai.voice.agents.breeze_buddy.assist.commerce.tenancy import (
    assist_tenant_candidates,
    normalize_merchant_domain,
)
from app.api.routers.breeze_buddy.widget_common import (
    client_ip,
    enforce_widget_ip_limit,
    enforce_widget_origin,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.widget_config import (
    get_widget_config_by_reseller_merchant,
)
from app.schemas.breeze_buddy.widget_config import StorefrontWidgetConfigResponse
from app.services.redis.rate_limit import check_rate_limit

_CACHE_TTL_SECONDS = 900

# Pre-lookup probe cap. The merchant-scoped limiter below can only run AFTER
# a config resolves (it is keyed by widget_config_id), which would leave
# unknown-domain probes as unbounded free DB lookups for an anonymous
# caller. This fixed per-IP cap bounds them. Generous on purpose: a real
# shopper hits this endpoint only on loader cache-miss (~4/shop/hour).
_PROBE_LIMIT_PER_IP_HOUR = 600
_PROBE_WINDOW_SECONDS = 3600


async def _enforce_probe_ip_limit(request: Request) -> None:
    decision = await check_rate_limit(
        bucket="storefront_probe",
        identifier=client_ip(request),
        limit=_PROBE_LIMIT_PER_IP_HOUR,
        window_seconds=_PROBE_WINDOW_SECONDS,
        prefix="widget",
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


async def storefront_widget_config_handler(
    request: Request, merchant_domain: str
) -> StorefrontWidgetConfigResponse:
    try:
        normalized_domain = normalize_merchant_domain(merchant_domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    # Before any DB read — see _PROBE_LIMIT_PER_IP_HOUR.
    await _enforce_probe_ip_limit(request)

    # The loader knows only the domain, not which host app installed
    # Assist, so probe both namespaces (standalone buddy-assist app first,
    # then the breeze-buddy tab's BB_SHOPIFY tenant). First ACTIVE config
    # wins — an inactive standalone tenant never shadows a live one.
    cfg = None
    for reseller_id, merchant_id in assist_tenant_candidates(normalized_domain):
        candidate = await get_widget_config_by_reseller_merchant(
            reseller_id, merchant_id
        )
        if candidate is not None and candidate.active:
            cfg = candidate
            break
    if cfg is None:
        # Unknown and inactive are indistinguishable — same posture as the
        # public-key path in widget_common.resolve_widget_config_for_request.
        logger.info(
            f"widget: no active storefront config for merchant_domain={normalized_domain}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Widget configuration not found",
        )

    enforce_widget_origin(request=request, cfg=cfg)

    # Separate bucket from "session_create": a page view must never burn a
    # real session slot. Same per-hour cap, its own counter.
    await enforce_widget_ip_limit(
        request=request,
        bucket="storefront_config",
        limit=cfg.max_sessions_per_ip_hour,
        widget_config_id=cfg.id,
    )

    return StorefrontWidgetConfigResponse(
        enabled=True,
        tenant=cfg.public_widget_key,
        merchant_domain=normalized_domain,
        appearance=cfg.appearance,
        settings_revision=(
            cfg.updated_at.isoformat() if cfg.updated_at is not None else None
        ),
        cache_ttl_seconds=_CACHE_TTL_SECONDS,
    )


__all__ = ["storefront_widget_config_handler"]
