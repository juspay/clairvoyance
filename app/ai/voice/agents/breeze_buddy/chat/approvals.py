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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.chat.block_codec import (
    tool_results_to_user_blocks,
)
from app.ai.voice.agents.breeze_buddy.template.approval import (
    LLM_STATUS_DENIED,
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
    decide_tool_approval,
    get_tool_approval,
    list_pending_tool_approvals,
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

# Wire status for a CLIENT-FULFILLED tool call resolved with data (not an
# approve/deny decision). Informational on ``function_approval_resolved``;
# the resumed stream is what the widget actually consumes.
WIRE_STATUS_FULFILLED = "fulfilled"


@dataclass
class ApprovalClaim:
    """Outcome of atomically claiming a HITL decision for one tool call.

    Shared by the HTTP ``/approval`` route (maps terminal outcomes to 404/409)
    and the voice bridge (maps them to RTVI events). ``outcome``:
    - ``proceed`` — the row was PENDING and is now claimed; the caller drives
      the resume turn (see ``run_chat_approval_continuation``).
    - ``not_found`` — no approval with that id for the session.
    - ``already_decided`` — the row was already resolved (``winning_status`` is
      its wire status so the client can settle the card).
    """

    outcome: str
    claimed: Optional[ToolApproval] = None
    effective_approved: bool = False
    wire_status: Optional[str] = None
    decision_reason: Optional[str] = None
    synthetic_result: Optional[Dict[str, Any]] = None
    pending_sibling_ids: List[str] = field(default_factory=list)
    expired_siblings: List[ToolApproval] = field(default_factory=list)
    winning_status: Optional[str] = None


async def claim_tool_approval(
    session_id: str,
    tool_call_id: str,
    approved: bool,
    reason: Optional[str],
) -> ApprovalClaim:
    """Atomically claim a pending HITL decision (the load-bearing DB step).

    MUST run under the session Redis lock. Mirrors the order the approval
    handler documents: claim the target row (a decision after ``expires_at``
    claims it EXPIRED → wire ``timeout``); for deny/expired persist the
    synthetic tool_result NOW so the history load sees a fully-answered batch;
    lazily expire other pending rows past their TTL; collect the still-pending
    sibling ids the resume turn must keep unanswered.

    Returns an :class:`ApprovalClaim`; the caller maps a non-``proceed``
    outcome to its transport (HTTP status vs RTVI event) and, on ``proceed``,
    drives :func:`run_chat_approval_continuation`.
    """
    row = await get_tool_approval(session_id, tool_call_id)
    if row is None:
        return ApprovalClaim(outcome="not_found")
    if row.status != ToolApprovalStatus.PENDING:
        return ApprovalClaim(
            outcome="already_decided",
            winning_status=WIRE_STATUS_BY_DB_STATUS.get(row.status),
        )

    is_expired = row.expires_at <= datetime.now(timezone.utc)
    if is_expired:
        new_status = ToolApprovalStatus.EXPIRED
        decision_reason: Optional[str] = "decision arrived after expiry"
    elif approved:
        new_status = ToolApprovalStatus.APPROVED
        decision_reason = reason
    else:
        new_status = ToolApprovalStatus.DENIED
        decision_reason = reason

    claimed = await decide_tool_approval(
        session_id, tool_call_id, new_status, decision_reason
    )
    if claimed is None:
        # Lost the claim race to a concurrent decision.
        relook = await get_tool_approval(session_id, tool_call_id)
        return ApprovalClaim(
            outcome="already_decided",
            winning_status=(
                WIRE_STATUS_BY_DB_STATUS.get(relook.status) if relook else None
            ),
        )

    effective_approved = approved and not is_expired
    wire_status = WIRE_STATUS_BY_DB_STATUS[new_status]
    logger.info(
        f"[approval] session={session_id} tool_call_id={tool_call_id} "
        f"function={claimed.function_name} decision={new_status.value}"
        + (f" reason={decision_reason!r}" if decision_reason else "")
    )

    synthetic_result: Optional[Dict[str, Any]] = None
    if not effective_approved:
        synthetic_result = (
            dict(EXPIRED_RESULT)
            if is_expired
            else {
                "status": LLM_STATUS_DENIED,
                "reason": reason or "the user did not approve this action",
            }
        )
        await insert_chat_message(
            session_id=session_id,
            role=ChatMessageRole.USER,
            content=None,
            content_blocks=tool_results_to_user_blocks(
                [(tool_call_id, synthetic_result)]
            ),
        )

    # Lazily expire the OTHER pending rows past their TTL (persists their
    # synthetic timeout results); the caller surfaces them at stream start.
    expired_siblings = await resolve_dangling_approvals(session_id, only_expired=True)
    # include_expired: a sibling whose expires_at falls between the sweep and
    # this read is still PENDING + decidable — keep it in the exclude set.
    pending_siblings = await list_pending_tool_approvals(
        session_id, include_expired=True
    )

    return ApprovalClaim(
        outcome="proceed",
        claimed=claimed,
        effective_approved=effective_approved,
        wire_status=wire_status,
        decision_reason=decision_reason,
        synthetic_result=synthetic_result,
        pending_sibling_ids=[sib.tool_call_id for sib in pending_siblings],
        expired_siblings=expired_siblings,
    )


async def claim_client_tool_result(
    session_id: str,
    tool_call_id: str,
    result: Dict[str, Any],
) -> ApprovalClaim:
    """Atomically claim a pending CLIENT-FULFILLED tool call and inject the
    frontend-supplied ``result`` as its tool_result.

    The client analogue of :func:`claim_tool_approval` (same load-bearing order:
    claim under the session Redis lock, persist the synthetic tool_result row
    BEFORE the history load so replay is fully answered, then sweep + collect
    siblings) — but the resolution carries DATA, not an approve/deny decision.

    The row is marked ``APPROVED`` (the call WAS fulfilled — there is no human
    refusal here, and the status CHECK forbids a new value), yet the returned
    claim sets ``effective_approved=False`` so the shared continuation
    (:func:`run_chat_approval_continuation`) injects ``synthetic_result`` from
    the replayed history instead of executing the function server-side —
    ``get_current_screen`` has no server-side execution; its result IS the
    client data.

    A call past ``expires_at`` is claimed ``EXPIRED`` with a timeout result,
    exactly like a late approval, so the LLM can acknowledge the miss.
    """
    row = await get_tool_approval(session_id, tool_call_id)
    if row is None:
        return ApprovalClaim(outcome="not_found")
    if row.status != ToolApprovalStatus.PENDING:
        return ApprovalClaim(
            outcome="already_decided",
            winning_status=WIRE_STATUS_BY_DB_STATUS.get(row.status),
        )

    is_expired = row.expires_at <= datetime.now(timezone.utc)
    new_status = (
        ToolApprovalStatus.EXPIRED if is_expired else ToolApprovalStatus.APPROVED
    )
    decision_reason: Optional[str] = (
        "result arrived after expiry" if is_expired else None
    )

    claimed = await decide_tool_approval(
        session_id, tool_call_id, new_status, decision_reason
    )
    if claimed is None:
        # Lost the claim race to a concurrent resolution.
        relook = await get_tool_approval(session_id, tool_call_id)
        return ApprovalClaim(
            outcome="already_decided",
            winning_status=(
                WIRE_STATUS_BY_DB_STATUS.get(relook.status) if relook else None
            ),
        )

    # Never executes server-side, so effective_approved is always False: an
    # expired call gets the timeout result, otherwise the client's captured data.
    synthetic_result: Dict[str, Any] = dict(EXPIRED_RESULT) if is_expired else result
    wire_status = (
        WIRE_STATUS_BY_DB_STATUS[ToolApprovalStatus.EXPIRED]
        if is_expired
        else WIRE_STATUS_FULFILLED
    )
    await insert_chat_message(
        session_id=session_id,
        role=ChatMessageRole.USER,
        content=None,
        content_blocks=tool_results_to_user_blocks([(tool_call_id, synthetic_result)]),
    )

    logger.info(
        f"[client_tool] session={session_id} tool_call_id={tool_call_id} "
        f"function={claimed.function_name} status={new_status.value}"
    )

    # Same sibling housekeeping as the approval claim: lazily expire other
    # pending rows past TTL, and collect those still pending so the resume keeps
    # them unanswered (their replayed tool_use would otherwise dangle).
    expired_siblings = await resolve_dangling_approvals(session_id, only_expired=True)
    pending_siblings = await list_pending_tool_approvals(
        session_id, include_expired=True
    )

    return ApprovalClaim(
        outcome="proceed",
        claimed=claimed,
        effective_approved=False,
        wire_status=wire_status,
        decision_reason=decision_reason,
        synthetic_result=synthetic_result,
        pending_sibling_ids=[sib.tool_call_id for sib in pending_siblings],
        expired_siblings=expired_siblings,
    )


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
