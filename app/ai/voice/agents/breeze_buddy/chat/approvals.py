"""Dangling-approval resolution for chat mode (HITL Pattern B).

When a chat turn ends awaiting approval, the assistant row with the gated
``tool_use`` block is already durably persisted — until a ``tool_result``
answers it, the history is invalid to replay. The functions here keep that
invariant under every non-happy path:

- A new user message supersedes everything pending (the user moved on).
- The approval handler lazily expires rows past ``expires_at``.

Both atomically claim PENDING rows and persist synthetic tool_result rows
in the same step, UNDER the session Redis lock and BEFORE the history is
loaded — so every history load sees a fully-answered batch.
"""

from typing import Any, Dict, List

from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.template.approval import (
    LLM_STATUS_NOT_DECIDED,
    LLM_STATUS_TIMEOUT,
    WIRE_STATUS_APPROVED,
    WIRE_STATUS_DENIED,
    WIRE_STATUS_SUPERSEDED,
    WIRE_STATUS_TIMEOUT,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import insert_chat_message
from app.database.accessor.breeze_buddy.tool_approvals import (
    claim_pending_tool_approvals,
)
from app.schemas.breeze_buddy.chat import (
    ChatMessageRole,
    ToolApproval,
    ToolApprovalStatus,
)

# LLM-visible synthetic results. Supersede is deliberately NOT "denied":
# the user sent an unrelated message instead of deciding, and labeling
# that a refusal invites the model to open its next reply with
# "since you declined X…".
SUPERSEDED_RESULT: Dict[str, Any] = {
    "status": LLM_STATUS_NOT_DECIDED,
    "reason": (
        "the user sent a new message instead of deciding; "
        "do not treat this as a refusal"
    ),
}
EXPIRED_RESULT: Dict[str, Any] = {
    "status": LLM_STATUS_TIMEOUT,
    "reason": "the user did not approve in time",
}

# DB status -> wire status (SSE/RTVI enum: approved|denied|timeout|
# cancelled|superseded). PENDING has no wire mapping by design.
WIRE_STATUS_BY_DB_STATUS: Dict[ToolApprovalStatus, str] = {
    ToolApprovalStatus.APPROVED: WIRE_STATUS_APPROVED,
    ToolApprovalStatus.DENIED: WIRE_STATUS_DENIED,
    ToolApprovalStatus.EXPIRED: WIRE_STATUS_TIMEOUT,
    ToolApprovalStatus.SUPERSEDED: WIRE_STATUS_SUPERSEDED,
}


async def resolve_dangling_approvals(
    session_id: str,
    *,
    only_expired: bool,
) -> List[ToolApproval]:
    """Claim PENDING rows and persist their synthetic tool_result rows.

    MUST run under the session Redis lock, BEFORE the turn's history load.

    - ``only_expired=False`` (``/message`` path): every pending row is
      SUPERSEDED with a not-decided result.
    - ``only_expired=True`` (approval-handler path): only rows past
      ``expires_at`` are EXPIRED with a timeout result; undecided siblings
      stay pending.

    Returns the claimed rows so the caller can emit
    ``function_approval_resolved`` events for them.
    """
    if only_expired:
        new_status = ToolApprovalStatus.EXPIRED
        reason = "expired before a decision arrived"
        payload: Dict[str, Any] = EXPIRED_RESULT
    else:
        new_status = ToolApprovalStatus.SUPERSEDED
        reason = "superseded by a new user message"
        payload = SUPERSEDED_RESULT

    claimed = await claim_pending_tool_approvals(
        session_id=session_id,
        new_status=new_status,
        reason=reason,
        only_expired=only_expired,
    )
    if not claimed:
        return []

    # One coalesced USER row answers the whole batch — all pending rows
    # always belong to the latest gate turn's single assistant batch, so
    # the replayed tool messages stay contiguous with their tool_calls.
    pairs = [(row.tool_call_id, payload) for row in claimed]
    await insert_chat_message(
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=None,
        content_blocks=tool_results_to_user_blocks(pairs),
    )
    logger.info(
        f"[approval] Resolved {len(claimed)} dangling approval(s) as "
        f"{new_status.value} for session {session_id}"
    )
    return claimed


async def terminate_pending_approvals(session_id: str) -> List[ToolApproval]:
    """Resolve ALL still-pending approvals when a session terminates.

    The chat counterpart of voice's ``ApprovalManager.deny_all`` on a terminal
    event (disconnect / idle / conversation-end). The idle-cleanup task ends a
    session, but without this its gated ``tool_approval`` rows are left PENDING
    until lazy expiry — a dangling ``tool_use`` a late reload or audit would
    see unanswered, and a ``pending_approvals`` query would report as still
    live on an ended session. Claims every pending row as EXPIRED (timeout
    result) and writes the coalesced synthetic ``tool_result`` so the
    dangling-tool_use invariant holds even on an ended session.

    Differs from ``resolve_dangling_approvals(only_expired=False)`` only in the
    terminal STATUS: that path SUPERSEDES (the user moved on, mid-session);
    this one EXPIRES (the session itself ended — closer to voice's terminal
    deny). The caller runs under the session Redis lock (see chat/cleanup.py),
    and the claim is an atomic CAS, so a racing ``/approval`` simply loses.
    """
    claimed = await claim_pending_tool_approvals(
        session_id=session_id,
        new_status=ToolApprovalStatus.EXPIRED,
        reason="the chat session ended before a decision arrived",
        only_expired=False,  # claim ALL pending, not just rows past expires_at
    )
    if not claimed:
        return []

    pairs = [(row.tool_call_id, EXPIRED_RESULT) for row in claimed]
    await insert_chat_message(
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=None,
        content_blocks=tool_results_to_user_blocks(pairs),
    )
    logger.info(
        f"[approval] Terminated {len(claimed)} pending approval(s) on session "
        f"end for {session_id}"
    )
    return claimed
