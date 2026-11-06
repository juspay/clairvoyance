"""An agent asks for a person (handoff_to_human, PR 3): the thread shows in
"Needs attention" and Buddy goes silent until a teammate takes it, the
claim SLA lapses (Buddy resumes, D5), or the window runs out.

Fail closed (D15): with human handoff off on the binding, the ask is refused
— the function is not even offered to the agent then (PR 3), so a refusal
here is the backstop, never the plan.
"""

from datetime import datetime
from typing import Optional

from app.core.logger import logger
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import thread as thread_accessor
from app.crm.conversations.errors import NotAllowed, ThreadConflict, ThreadNotFound
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import HandoffRead, Thread
from app.crm.conversations.state import lapsed
from app.crm.conversations.status import (
    HANDOFF_PRIORITIES,
    OUTCOME_SLA_LAPSED,
    PRIORITY_NORMAL,
    TRAIL_ACTION_HANDOFF,
    WAKE_HANDOFF,
)
from app.crm.conversations.threads import get_open_handoff, settings_for, thread_or_404


async def request_handoff(
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str] = None,
    summary: Optional[str] = None,
    priority: str = PRIORITY_NORMAL,
) -> HandoffRead:
    """Open the thread's handoff — or return the one already open: asking
    twice is the same ask (idempotent). Only the session answering the
    thread may ask (checked again under the row lock, below)."""
    thread = await thread_or_404(merchant_id, thread_id)
    _check_can_request_handoff(thread, chat_session_id)
    if not (await settings_for(thread)).human_handoff:
        raise NotAllowed("human handoff is off for this connection")
    return await atomically(
        _request_handoff_in_txn,
        merchant_id,
        thread_id,
        chat_session_id,
        reason,
        summary,
        priority if priority in HANDOFF_PRIORITIES else PRIORITY_NORMAL,
    )


async def _request_handoff_in_txn(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str],
    summary: Optional[str],
    priority: str,
) -> HandoffRead:
    """ATOMIC: the handoff (four columns on the thread) and its wake-up
    share fate, under the thread's row lock — a take-over that lands first
    makes the ask refuse, one that lands after claims it; a thread holds at
    most one open handoff, so a second ask finds the first."""
    thread = await thread_accessor.get_thread(
        merchant_id, thread_id, txn, for_update=True
    )
    if thread is None:
        raise ThreadNotFound("no such conversation")
    _check_can_request_handoff(thread, chat_session_id)
    opened = await thread_accessor.open_handoff(
        txn, merchant_id, thread_id, reason, summary, priority
    )
    if opened is None:
        # Already open: the thread read under the lock above carries it.
        existing = get_open_handoff(thread)
        if existing is None:
            raise RuntimeError(f"thread {thread_id}: handoff neither opened nor open")
        return existing
    await wake(merchant_id, thread_id, WAKE_HANDOFF, txn)
    handoff = get_open_handoff(opened)
    if handoff is None:
        raise RuntimeError(f"thread {thread_id}: handoff opened but not read back")
    return handoff


def _check_can_request_handoff(thread: Thread, chat_session_id: str) -> None:
    """Fail closed: only the session answering an open thread nobody else
    holds may ask for a person on it."""
    if thread.resolved_at is not None:
        raise ThreadConflict("this conversation is resolved")
    if thread.assignee_user_id is not None:
        raise ThreadConflict("a teammate already holds this conversation")
    if thread.bot_session_id != chat_session_id:
        raise NotAllowed("this chat is not the one answering the conversation")


async def can_request_handoff(
    merchant_id: str, thread_id: str, chat_session_id: str
) -> bool:
    """May this session ask for a person on this thread right now? Fail
    closed: no thread, a resolved or taken one, a session that isn't the
    thread's, or handoff switched off on the binding — no."""
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return False
    try:
        _check_can_request_handoff(thread, chat_session_id)
    except (ThreadConflict, NotAllowed):
        return False
    return (await settings_for(thread)).human_handoff


async def close_handoff_if_lapsed(thread: Thread, now: datetime) -> bool:
    """Close a thread's unclaimed handoff past its claim SLA (D5) — Buddy
    takes the thread back. True when this call closed it."""
    if thread.handoff_requested_at is None:
        return False
    sla = (await settings_for(thread)).claim_sla_minutes
    if not lapsed(thread, sla, now):
        return False
    closed = await thread_accessor.close_lapsed_handoff(
        thread.merchant_id,
        thread.id,
        thread.handoff_requested_at,
        TRAIL_ACTION_HANDOFF,
        OUTCOME_SLA_LAPSED,
        sla,
    )
    if closed is None:
        return False
    await wake(thread.merchant_id, thread.id, WAKE_HANDOFF)
    logger.bind(merchant_id=thread.merchant_id, thread_id=thread.id).info(
        f"handoff on thread {thread.id} lapsed after {sla} min unclaimed — "
        f"Buddy resumes"
    )
    return True
