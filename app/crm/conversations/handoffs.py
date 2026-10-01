"""An agent asks for a person (handoff_to_human, PR 3): the thread shows in
"Needs attention" and Buddy goes silent until a teammate takes it, the
claim SLA lapses (Buddy resumes, D5), or the window runs out.

Fail closed (D15): with human handoff off on the number, the ask is refused
— the function is not even offered to the agent then (PR 3), so a refusal
here is the backstop, never the plan.
"""

from datetime import datetime, timezone
from typing import Optional

from app.core.logger import logger
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.errors import NotAllowed, ThreadConflict
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import Handoff
from app.crm.conversations.state import lapsed
from app.crm.conversations.status import OUTCOME_SLA_LAPSED, WAKE_HANDOFF
from app.crm.conversations.threads import settings_for, thread_or_404

#: The priority words an agent may pass (vocabulary, no CHECK).
PRIORITIES = ("normal", "urgent")


async def request_handoff(
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str] = None,
    summary: Optional[str] = None,
    priority: str = "normal",
) -> Handoff:
    """Open the thread's handoff — or return the one already open: asking
    twice is the same ask (idempotent)."""
    thread = await thread_or_404(merchant_id, thread_id)
    if thread.resolved_at is not None:
        raise ThreadConflict("this conversation is resolved")
    if not (await settings_for(thread)).human_handoff:
        raise NotAllowed("human handoff is off for this number")
    return await atomically(
        _request_handoff_in_txn,
        merchant_id,
        thread_id,
        chat_session_id,
        reason,
        summary,
        priority if priority in PRIORITIES else "normal",
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
    """ATOMIC: the handoff and its wake-up share fate; the partial unique
    (one open per thread) makes a second ask find the first."""
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
    closed = await handoff_accessor.close(
        None, handoff.merchant_id, handoff.conversation_id, OUTCOME_SLA_LAPSED, None
    )
    if closed is None:
        return False
    await wake(handoff.merchant_id, handoff.conversation_id, WAKE_HANDOFF)
    logger.bind(
        merchant_id=handoff.merchant_id, thread_id=handoff.conversation_id
    ).info(f"handoff {handoff.id} lapsed after {sla} min unclaimed — Buddy resumes")
    return True


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
