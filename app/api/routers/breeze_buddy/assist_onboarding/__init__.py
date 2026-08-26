"""Authenticated SSE onboarding for Buddy Assist."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.ai.voice.agents.breeze_buddy.assist.commerce.assist_onboarding import (
    stream_assist_onboarding,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import format_sse
from app.api.security.breeze_buddy.authorization import (
    validate_merchant_access,
    validate_reseller_access,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_role
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.assist_onboarding import AssistOnboardingStreamRequest

router = APIRouter()

# A merchant may onboard themselves — that is the product. The two scope checks
# on the route below already bind any caller to their own reseller and merchant,
# so the role list is the whole gate: it keeps out `user`, and nothing else.
_ONBOARDING_ROLES = [UserRole.ADMIN, UserRole.RESELLER, UserRole.MERCHANT]


async def _sse_body(body: AssistOnboardingStreamRequest) -> AsyncIterator[str]:
    async for event in stream_assist_onboarding(body):
        yield format_sse(event)


@router.post("/assist/onboard/stream")
async def onboard_assist_stream(
    body: AssistOnboardingStreamRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> StreamingResponse:
    """Create or refresh one merchant's Assist template and widget."""
    require_role(current_user, _ONBOARDING_ROLES)
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


__all__ = ["router"]
