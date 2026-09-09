"""``POST /assist/probe`` — look at a site once and report what it is.

The first step of onboarding and, later, of an on-demand preview: before
anything is built, an operator sees which platform we recognised, how sure we
are, and the evidence behind it.

The caller chooses the URL, so this is the one route that makes the server
fetch an address someone else picked. The engine's fetch guard is what makes
that safe (https only, public addresses only, every redirect re-validated,
hard byte and time caps); this handler adds who may ask and how often.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.assist.engine.classify import classify_profile
from app.ai.voice.agents.breeze_buddy.assist.engine.probe import probe_site, summarize
from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    EgressNotGuardedError,
    FetchFailedError,
    UnsafeUrlError,
)
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.logger import logger
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.probe import (
    ProbeRequest,
    ProbeResponse,
    build_report,
)
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# An outbound fetch on someone else's infrastructure, so the same gate as the
# other route that spends platform resources on a caller's behalf.
_PROBE_ROLES = [UserRole.ADMIN, UserRole.RESELLER]
# Per caller, not per IP: these are authenticated operators, and the cap is
# here to stop a script pointing us at a list of hosts, not to ration normal
# onboarding (which probes a site once or twice).
_PROBE_LIMIT_PER_HOUR = 120
_PROBE_WINDOW_SECONDS = 3600
_PROBE_TIMEOUT_SECONDS = 12.0


@router.post("/assist/probe", response_model=ProbeResponse)
async def probe_website(
    body: ProbeRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ProbeResponse:
    """Fetch the URL's home page once and report what the site appears to run on.

    400 when the URL is not a public https address or the site could not be
    read; 429 when the caller is over the hourly cap; 503 when this
    deployment cannot make a guarded outbound request at all.
    """
    require_role(current_user, _PROBE_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)

    decision = await check_rate_limit(
        bucket="assist_probe",
        identifier=current_user.id,
        limit=_PROBE_LIMIT_PER_HOUR,
        window_seconds=_PROBE_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Probe rate limit hit ({decision.count}/{decision.limit} per hour). "
                "Try again later."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    try:
        profile = await probe_site(body.url, timeout_seconds=_PROBE_TIMEOUT_SECONDS)
    except EgressNotGuardedError as exc:
        # Nothing the caller did: this deployment cannot make the request
        # safely, so the feature is off here rather than weakened.
        logger.error(f"assist probe unavailable: {exc}")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    except FetchFailedError as exc:
        # The site, not us: a bad host, a refused connection, a timeout. The
        # operator needs to see which, so the reason is passed through.
        logger.info(f"assist probe could not read the site: {exc}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    classification = classify_profile(profile)
    logger.info(
        "assist probe complete",
        platform=classification.adapter_id,
        confidence=classification.confidence,
        challenge=profile.challenge,
        status=profile.status,
    )
    return build_report(profile, classification, summarize(profile))


__all__ = ["router"]
