"""``POST /assist/brand`` — how a site looks, so an agent can look like it.

Split from the probe deliberately. Recognising a site is one fetch and answers
in about a second; reading its brand renders the page and can take the better
part of a minute. Keeping them apart lets a console name the platform
immediately and fill in the colours when they arrive, instead of making the
operator wait on the slower question to learn the faster answer.

Everything returned is a proposal. The measurements behind the chain say no
source is reliable enough to apply unseen, so the console shows these with
their provenance and the merchant confirms.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.engine.classify import classify_profile
from app.ai.voice.agents.breeze_buddy.assist.engine.probe import probe_site
from app.ai.voice.agents.breeze_buddy.assist.engine.research import brand as brand_lane
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.brand import BrandRequest, BrandResponse
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

_BRAND_ROLES = [UserRole.ADMIN, UserRole.RESELLER]
# Lower than the probe's cap: each of these renders a page somewhere else.
_BRAND_LIMIT_PER_HOUR = 60
_BRAND_WINDOW_SECONDS = 3600


@router.post("/assist/brand", response_model=BrandResponse)
async def read_brand(
    body: BrandRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> BrandResponse:
    """Colours, fonts and logo for a site, each with the source that said so.

    400 for a URL we may not fetch or a site we could not read; 429 over the
    hourly cap; 503 where this deployment cannot make a guarded request.
    """
    require_role(current_user, _BRAND_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)

    decision = await check_rate_limit(
        bucket="assist_brand",
        identifier=current_user.id,
        limit=_BRAND_LIMIT_PER_HOUR,
        window_seconds=_BRAND_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Brand rate limit hit ({decision.count}/{decision.limit} per hour). "
                "Try again later."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        profile = await probe_site(body.url)
        adapter = registry.resolve(classify_profile(profile).adapter_id)
        look = await brand_lane.resolve(
            profile.final_url,
            profile,
            platform_look=await adapter.brand(profile),
            # Only the platform this site actually runs on. The union across
            # every adapter threw away a real brand purple that sat 32 units
            # from another platform's own brand colour — inside the distance
            # the guard calls "the same colour" — on a site that platform has
            # nothing to do with.
            stock_colors=adapter.stock_colors(),
            use_provider=body.use_provider,
        )
    except EgressNotGuardedError as exc:
        logger.error(f"assist brand unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (UnsafeUrlError, FetchFailedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    logger.info(
        "assist brand resolved",
        colors=len(look.colors),
        sources=look.sources,
        warnings=len(look.warnings),
    )
    return BrandResponse.from_look(look)


__all__ = ["router"]
