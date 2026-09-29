"""``POST /assist/onboarding/research/stream``: read a store's site and stream
back the facts found, each with the page it came from."""

from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.assist.engine.web.fetch import (
    UnsafeUrlError,
    normalize_probe_url,
)
from app.ai.voice.agents.breeze_buddy.assist.onboarding.research.stream import (
    research_events,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import format_sse
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist.probe import ProbeRequest
from app.services.redis.rate_limit import check_rate_limit

router = APIRouter()

# A merchant researches a store from the console: the merchant_id it must send
# is checked against its scope. The URL cannot be tied to that merchant (a new
# one has no store on record), so the daily cap below bounds the cost.
_RESEARCH_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]
# Runs a day per caller: the costliest call on the assist surface (a site map
# and up to 6 rendered page reads, about 31 Firecrawl credits).
_RUNS_PER_USER_PER_DAY = 10
_RESEARCH_WINDOW_SECONDS = 24 * 3600


@router.post("/assist/onboarding/research/stream")
async def research_site_stream(
    body: ProbeRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """Read the site at ``body.url`` and stream what was found.

    Events: ``progress`` {step, status, detail}, ``note`` {field, value,
    source_url}, ``ping`` {} while quiet, then exactly one of ``done``
    {success, status} or ``error`` {success, message, retryable}.
    """
    require_role(current_user, _RESEARCH_ROLES)
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    if current_user.role == UserRole.MERCHANT and not body.merchant_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="merchant_id is required for a merchant login",
        )
    if body.merchant_id:
        validate_merchant_access(current_user, merchant_id=body.merchant_id)
    try:
        url = normalize_probe_url(body.url)
    except UnsafeUrlError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc

    decision = await check_rate_limit(
        bucket="assist_research_user",
        identifier=current_user.id,
        limit=_RUNS_PER_USER_PER_DAY,
        window_seconds=_RESEARCH_WINDOW_SECONDS,
        prefix="assist",
        fail_closed=True,
    )
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Research limit reached ({decision.limit} runs a day). "
                "Try again tomorrow."
            ),
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )

    return StreamingResponse(
        _sse_body(url),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


async def _sse_body(url: str) -> AsyncIterator[str]:
    async for event in research_events(url):
        yield format_sse(event)


__all__ = ["router"]
