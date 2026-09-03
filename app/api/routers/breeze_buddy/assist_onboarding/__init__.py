"""Authenticated SSE onboarding for Buddy Assist."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.assist.commerce.assist_onboarding import (
    OnboardingFailure,
    onboard_assist_bare,
    stream_assist_onboarding,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.tenancy import (
    assist_tenant,
    normalize_merchant_domain,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import format_sse
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist_onboarding import (
    AssistOnboardingStreamRequest,
    AssistOnboardRequest,
    AssistOnboardResponse,
)

router = APIRouter()


async def _sse_body(body: AssistOnboardingStreamRequest) -> AsyncIterator[str]:
    async for event in stream_assist_onboarding(body):
        yield format_sse(event)


@router.post("/assist/onboard/stream")
async def onboard_assist_stream(
    body: AssistOnboardingStreamRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """Create or refresh one merchant's Assist template and widget."""
    require_role(current_user, [UserRole.ADMIN, UserRole.RESELLER])
    validate_reseller_access(current_user, reseller_id=body.reseller_id)
    validate_merchant_access(current_user, merchant_id=body.merchant_id)

    return StreamingResponse(
        _sse_body(body),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/assist/onboard", response_model=AssistOnboardResponse)
async def onboard_assist(
    body: AssistOnboardRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> AssistOnboardResponse:
    """Bare-metal install-time onboarding (S2S, no personalization).

    Same RBAC posture as the stream route; tenancy is derived
    server-side from ``host_app`` + ``merchant_domain`` (breeze-buddy →
    BB_SHOPIFY/plain domain; buddy-assist → BB_ASSIST/assist-prefixed),
    so access is validated against the DERIVED ids — a caller cannot
    smuggle a different namespace in.
    """
    require_role(current_user, [UserRole.ADMIN, UserRole.RESELLER])
    try:
        merchant_domain = normalize_merchant_domain(body.merchant_domain)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    reseller_id, merchant_id = assist_tenant(body.host_app, merchant_domain)
    validate_reseller_access(current_user, reseller_id=reseller_id)
    validate_merchant_access(current_user, merchant_id=merchant_id)

    try:
        return await onboard_assist_bare(body)
    except OnboardingFailure as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "step": exc.step,
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
            },
        ) from exc


__all__ = ["router"]
