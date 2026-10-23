"""Buddy's inbox answer route — how a customer's message on a thread Buddy
holds reaches Buddy on the API pods (D41).

``POST /inbox/threads/{thread_id}/answer`` — conversations calls it once her
message is on a thread Buddy holds, the way the widget POSTs its message, and
when Buddy is given a thread back (a hand back, a lapsed handoff: the
``reason`` Buddy is told). The turn runs here in the background and the call
returns at once (202): the caller is a worker or a teammate's request, not a
person watching a stream. Auth is the platform's RBAC access token, as on
every Buddy route — the caller mints a short-lived token for the merchant —
and only a holder of that merchant passes.
"""

from typing import Dict, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, field_validator

from app.ai.voice.agents.breeze_buddy.chat.inbox.answer import start_answer
from app.api.security.breeze_buddy.authorization import validate_merchant_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.crm.conversations.contracts import RESUME_REASONS
from app.schemas import UserInfo

router = APIRouter(prefix="/inbox", tags=["inbox"])


class AnswerRequest(BaseModel):
    """The merchant the thread belongs to — named in the request, never
    inferred from the token (a token may hold several)."""

    merchant_id: str = Field(..., min_length=1, max_length=255)
    #: Why Buddy has the thread back (a hand back, a claim timeout), if it
    #: was just given back.
    reason: Optional[str] = None

    @field_validator("reason")
    @classmethod
    def _known_reason(cls, reason: Optional[str]) -> Optional[str]:
        if reason is not None and reason not in RESUME_REASONS:
            raise ValueError(f"unknown reason {reason!r}")
        return reason


@router.post("/threads/{thread_id}/answer", status_code=status.HTTP_202_ACCEPTED)
async def answer_thread_route(
    # Typed, so a malformed id is a 422 here, never a cast error later.
    thread_id: UUID,
    req: AnswerRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> Dict[str, bool]:
    """Start Buddy's answer to the thread; returns before the turn runs."""
    validate_merchant_access(current_user, merchant_id=req.merchant_id)
    start_answer(req.merchant_id, str(thread_id), req.reason)
    return {"accepted": True}
