"""``POST /assist/onboarding/preview``: a store's brand colours and logo, for
the console.

Read-only: detects the look and saves nothing. Slower than the probe (a
rendered read can take most of a minute), so it has its own daily cap, the
same as research's.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.engine.probe import probe_site
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.branding import (
    read_branding,
)
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.onboarding.preview import PreviewResponse
from app.schemas.breeze_buddy.assist.probe import ProbeRequest
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# A merchant previews a store from the console: the merchant_id it must send
# is checked against its scope. The URL cannot be tied to that merchant (a new
# one has no store on record), so the daily cap bounds the cost.
_PREVIEW_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]
# Previews a day per caller: about 5 Firecrawl credits each, and one per
# research run (10 a day) is all onboarding needs.
_PREVIEWS_PER_USER_PER_DAY = 10
_PREVIEW_WINDOW_SECONDS = 24 * 3600


@router.post("/assist/onboarding/preview", response_model=PreviewResponse)
async def preview_look(
    body: ProbeRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> PreviewResponse:
    """Detect a site's brand colours and logo without saving anything.

    400 for a URL we may not fetch or a site we could not read; 429 over the
    daily cap; 503 where this deployment cannot make a guarded request. A
    missing brand provider is not an error: the response has no colours.
    """
    require_role(current_user, _PREVIEW_ROLES)
    if current_user.role == UserRole.MERCHANT and not body.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="merchant_id is required for a merchant login",
        )
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)

    decision = await check_rate_limit(
        bucket="assist_preview",
        identifier=current_user.id,
        limit=_PREVIEWS_PER_USER_PER_DAY,
        window_seconds=_PREVIEW_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Preview limit reached ({decision.limit} a day). "
                "Try again tomorrow."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        profile = await probe_site(body.url)
    except EgressNotGuardedError as exc:
        logger.error(f"assist preview unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (UnsafeUrlError, FetchFailedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    look = await read_branding(profile.final_url)

    logger.info(
        "assist preview resolved",
        primary_color=look.primary_color,
        logo=bool(look.logo_url),
    )
    return PreviewResponse(**look._asdict())


__all__ = ["router"]
