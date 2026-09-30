"""``POST /assist/preview``: a store's brand colours and logo, for the console.

Read-only: detects the look and saves nothing. Slower than the probe (a
rendered read can take most of a minute), so it has its own, lower hourly cap.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.look import read_look
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.preview import PreviewResponse
from app.schemas.breeze_buddy.assist.probe import ProbeRequest
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# A merchant previews a store from the console: the merchant_id it must send
# is checked against its scope. The URL cannot be tied to that merchant (a new
# one has no store on record), so the hourly cap bounds the cost.
_PREVIEW_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]
_PREVIEW_LIMIT_PER_HOUR = 60
_PREVIEW_WINDOW_SECONDS = 3600
# The console waits 90 s for this answer; the read is done well inside that.
_PREVIEW_BUDGET_SECONDS = 75.0


@router.post("/assist/preview", response_model=PreviewResponse)
async def preview_look(
    body: ProbeRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> PreviewResponse:
    """Detect a site's brand colours and logo without saving anything.

    400 for a URL we may not fetch or a site we could not read; 429 over the
    hourly cap; 503 where this deployment cannot make a guarded request. A
    missing brand provider is not an error: the response says so in
    ``warnings`` and uses the logo instead.
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
        limit=_PREVIEW_LIMIT_PER_HOUR,
        window_seconds=_PREVIEW_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Preview rate limit hit ({decision.count}/{decision.limit} per "
                "hour). Try again later."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        look = await read_look(body.url, budget_seconds=_PREVIEW_BUDGET_SECONDS)
    except EgressNotGuardedError as exc:
        logger.error(f"assist preview unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (UnsafeUrlError, FetchFailedError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    logger.info(
        "assist preview resolved",
        colors=len(look.colors),
        logo=bool(look.logo_url),
        warnings=len(look.warnings),
    )
    return PreviewResponse(
        colors=look.colors,
        logo_url=look.logo_url,
        icon_url=look.icon_url,
        warnings=look.warnings,
    )


__all__ = ["router"]
