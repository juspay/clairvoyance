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
from app.crm.conversations.ask import ask_buddy
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.errors import NotAllowed, ThreadConflict, ThreadNotFound
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import Handoff, Thread
from app.crm.conversations.state import bot_answers, lapsed
from app.crm.conversations.status import (
    CHANNEL_WIDGET,
    HANDOFF_PRIORITIES,
    OUTCOME_SLA_LAPSED,
    PRIORITY_NORMAL,
    RESUME_CLAIM_TIMEOUT,
    WAKE_HANDOFF,
)
from app.crm.conversations.threads import settings_for, thread_or_404, window_of


async def request_handoff(
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str] = None,
    summary: Optional[str] = None,
    priority: str = PRIORITY_NORMAL,
) -> Handoff:
    """Open the thread's handoff — or return the one already open: asking
    twice is the same ask (idempotent). Only the session answering the
    thread may ask (checked again under the row lock, below)."""
    thread = await thread_or_404(merchant_id, thread_id)
    _may_ask(thread, chat_session_id)
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
) -> Handoff:
    """ATOMIC: the handoff and its wake-up share fate, under the thread's
    row lock — a take-over that lands first makes the ask refuse, one that
    lands after claims it; the partial unique (one open per thread) makes a
    second ask find the first."""
    thread = await thread_accessor.get_thread(
        merchant_id, thread_id, txn, for_update=True
    )
    if thread is None:
        raise ThreadNotFound("no such conversation")
    _may_ask(thread, chat_session_id)
    opened = await handoff_accessor.open_handoff(
        txn, merchant_id, thread_id, chat_session_id, reason, summary, priority
    )
    if opened is None:
        existing = await handoff_accessor.open_for_thread(merchant_id, thread_id, txn)
        if existing is None:
            raise RuntimeError(f"thread {thread_id}: handoff neither opened nor open")
        return existing
    await wake(merchant_id, thread_id, WAKE_HANDOFF, txn)
    return opened


def _may_ask(thread: Thread, chat_session_id: str) -> None:
    """Fail closed: only the session answering an open thread nobody else
    holds may ask for a person on it."""
    if thread.resolved_at is not None:
        raise ThreadConflict("this conversation is resolved")
    if thread.assignee_user_id is not None:
        raise ThreadConflict("a teammate already holds this conversation")
    if thread.bot_session_id != chat_session_id:
        raise NotAllowed("this chat is not the one answering the conversation")


async def handoff_available(
    merchant_id: str, thread_id: str, chat_session_id: str
) -> bool:
    """May this session ask for a person on this thread right now? Fail
    closed: no thread, a resolved or taken one, a session that isn't the
    thread's, or handoff switched off on the binding — no."""
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return False
    try:
        _may_ask(thread, chat_session_id)
    except (ThreadConflict, NotAllowed):
        return False
    return (await settings_for(thread)).human_handoff


async def lapse_if_due(handoff: Handoff, now: datetime) -> bool:
    """Close an unclaimed handoff past its claim SLA (D5) — Buddy takes the
    thread back. True when this call closed it."""
    thread = await thread_accessor.get_thread(
        handoff.merchant_id, handoff.conversation_id
    )
    if thread is None:
        return False
    sla = (await settings_for(thread)).claim_sla_minutes
    if not lapsed(handoff, sla, now):
        return False
    closed = await handoff_accessor.lapse(
        handoff.merchant_id, handoff.id, OUTCOME_SLA_LAPSED, sla
    )
    if closed is None:
        return False
    await wake(handoff.merchant_id, handoff.conversation_id, WAKE_HANDOFF)
    logger.bind(
        merchant_id=handoff.merchant_id, thread_id=handoff.conversation_id
    ).info(f"handoff {handoff.id} lapsed after {sla} min unclaimed — Buddy resumes")
    if (
        thread.channel != CHANNEL_WIDGET
        and bot_answers(thread)
        and window_of(thread, now).open
    ):
        # Buddy is told nobody came, in a turn of its own, with whatever she
        # wrote while she waited (D5, D38).
        await ask_buddy(
            handoff.merchant_id, handoff.conversation_id, RESUME_CLAIM_TIMEOUT
        )
    return True
