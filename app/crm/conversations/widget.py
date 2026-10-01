"""The website widget in the Inbox — backend only for now (the widget's own
UI comes later). A widget conversation has no spine letters and no window:
its thread is opened when its agent hands off, the shopper's messages are
appended while a teammate holds it, and the teammate's replies reach the
widget through its stream (PR 3) from ``widget_messages_after``.
"""

from datetime import datetime, timezone
from typing import List, Optional

from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.errors import ThreadNotFound
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import Thread, TimelineRow
from app.crm.conversations.status import (
    AUTHOR_CUSTOMER,
    AUTHOR_TEAMMATE,
    CHANNEL_WIDGET,
    KIND_INBOUND,
    KIND_OUTBOUND,
    WAKE_MESSAGE,
    WAKE_STATE,
)

TURN_ROWS_MAX = 50


def contact_key(session_id: str, customer_id: Optional[str]) -> str:
    """PURE: a known shopper is keyed by customer, an anonymous visitor by
    their session (D2) — lowercase, as the table's format CHECK insists."""
    return customer_id.lower() if customer_id else f"session:{session_id.lower()}"


async def open_widget_thread(
    merchant_id: str, session_id: str, agent_id: str, customer_id: Optional[str] = None
) -> Thread:
    """The thread a widget session hands off on — created, or reopened."""
    return await atomically(
        _open_widget_thread_in_txn, merchant_id, session_id, agent_id, customer_id
    )


async def _open_widget_thread_in_txn(
    txn: DbTxn,
    merchant_id: str,
    session_id: str,
    agent_id: str,
    customer_id: Optional[str],
) -> Thread:
    """ATOMIC: the thread exists and is held by this session's agent in one
    step — a teammate never sees a widget thread nobody holds."""
    thread = await thread_accessor.ensure_thread(
        txn,
        merchant_id,
        CHANNEL_WIDGET,
        contact_key(session_id, customer_id),
        customer_id,
        None,
        None,
    )
    started = await thread_accessor.start_bot(
        txn, merchant_id, thread.id, agent_id, session_id
    )
    if started is None:
        raise RuntimeError(f"widget thread {thread.id} vanished")
    await wake(merchant_id, thread.id, WAKE_STATE, txn)
    return started


async def append_widget_inbound(
    merchant_id: str, thread_id: str, text: str
) -> TimelineRow:
    """A shopper's message while the agent may not speak (a teammate holds
    the thread, or one is being waited for)."""
    return await atomically(_append_widget_inbound_in_txn, merchant_id, thread_id, text)


async def _append_widget_inbound_in_txn(
    txn: DbTxn, merchant_id: str, thread_id: str, text: str
) -> TimelineRow:
    """ATOMIC: the row and the thread's list fields move together."""
    thread = await thread_accessor.get_thread(
        merchant_id, thread_id, txn, for_update=True
    )
    if thread is None:
        raise ThreadNotFound("no such conversation")
    now = datetime.now(timezone.utc)
    row = await message_accessor.insert_inbound(
        txn,
        merchant_id,
        thread_id,
        KIND_INBOUND,
        AUTHOR_CUSTOMER,
        None,
        None,
        {"type": "text", "text": text},
        now,
    )
    if row is None:
        raise RuntimeError(f"widget message on thread {thread_id} was not written")
    await thread_accessor.record_inbound(
        txn,
        merchant_id,
        thread_id,
        now,
        text,
        False,
        thread.bot_template_id,
        thread.bot_session_id,
    )
    await wake(merchant_id, thread_id, WAKE_MESSAGE, txn)
    return row


async def widget_messages_after(
    merchant_id: str, thread_id: str, after: Optional[datetime]
) -> List[TimelineRow]:
    """The teammate's replies the widget has not shown yet, oldest first."""
    rows = await message_accessor.rows_after(
        merchant_id, thread_id, [KIND_OUTBOUND], after, TURN_ROWS_MAX
    )
    return [row for row in rows if row.author_kind == AUTHOR_TEAMMATE]
