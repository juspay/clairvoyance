"""What Buddy's WhatsApp responder (PR 3, buddy-side) works through — the
thread side of a turn. The responder never reads crm tables; it asks here.

    claim_bot_work      threads with messages Buddy has not answered,
                        leased to one responder for a turn
    bot_may_speak       re-checked before EVERY send: nobody took the
                        thread, no person is being waited for (bar the
                        handoff this very session just asked for — its
                        waiting message still goes), and it is still on
                        Buddy's number
    set_bot_session     the chat_session answering this window
    pending_inbound     what she said since Buddy last answered
    human_era_slice     what was said while a teammate held it (context
                        for the first turn after a hand back)
    record_bot_reply    Buddy's reply onto the timeline (read-your-writes)
    mark_bot_cursor     the turn answered up to here; the lease goes
    thread_by_id        the thread as it stands (session reconciliation)
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config.static import CRM_INBOX_BOT_LEASE_SECONDS
from app.crm.connectivity.contracts import buddy_number
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import BotWork, Thread, TimelineRow
from app.crm.conversations.state import held_by
from app.crm.conversations.status import (
    AUTHOR_ASSIST,
    CHANNEL_WIDGET,
    HELD_BY_BUDDY,
    HELD_WAITING,
    KIND_INBOUND,
    KIND_OUTBOUND,
    WAKE_MESSAGE,
)
from app.crm.conversations.threads import settings_for

#: Rows one turn reads at most.
TURN_ROWS_MAX = 50


def _work(thread: Thread) -> Optional[BotWork]:
    if (
        thread.bot_template_id is None
        or thread.last_inbound_at is None
        or thread.bot_lease_until is None
    ):
        return None
    return BotWork(
        thread_id=thread.id,
        merchant_id=thread.merchant_id,
        channel=thread.channel,
        customer_id=thread.customer_id,
        address=thread.address,
        binding_id=thread.binding_id,
        agent_id=thread.bot_template_id,
        session_id=thread.bot_session_id,
        cursor_at=thread.bot_cursor_at,
        last_inbound_at=thread.last_inbound_at,
        lease_until=thread.bot_lease_until,
    )


async def claim_bot_work(
    channels: List[str], batch: int, settle_seconds: int = 0
) -> List[BotWork]:
    """Lease up to ``batch`` threads on ``channels`` for one turn each,
    across merchants — only once her last message is ``settle_seconds`` old
    (a burst is one turn)."""
    threads = await thread_accessor.claim_bot_work(
        CRM_INBOX_BOT_LEASE_SECONDS, settle_seconds, channels, batch
    )
    return [work for work in map(_work, threads) if work is not None]


async def bot_may_speak(
    merchant_id: str, thread_id: str, session_id: Optional[str] = None
) -> bool:
    """Fail closed: anything but "Buddy holds it, on Buddy's number" is no —
    except that ``session_id``'s own fresh handoff still lets it finish the
    turn that asked for it (the waiting message)."""
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return False
    handoff = await handoff_accessor.open_for_thread(merchant_id, thread_id)
    sla = (await settings_for(thread)).claim_sla_minutes
    held = held_by(thread, handoff, sla, datetime.now(timezone.utc))
    own_handoff = (
        held == HELD_WAITING
        and session_id is not None
        and handoff is not None
        and handoff.chat_session_id == session_id
    )
    if held != HELD_BY_BUDDY and not own_handoff:
        return False
    if thread.channel == CHANNEL_WIDGET:
        return True
    buddy = await buddy_number(merchant_id, thread.channel)
    return buddy is not None and buddy.id == thread.binding_id


async def set_bot_session(
    merchant_id: str, thread_id: str, session_id: str
) -> Optional[Thread]:
    return await thread_accessor.set_bot_session(merchant_id, thread_id, session_id)


async def thread_for_session(merchant_id: str, session_id: str) -> Optional[Thread]:
    return await thread_accessor.thread_for_session(merchant_id, session_id)


async def pending_inbound(merchant_id: str, thread_id: str) -> List[TimelineRow]:
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return []
    return await message_accessor.rows_after(
        merchant_id, thread_id, [KIND_INBOUND], thread.bot_cursor_at, TURN_ROWS_MAX
    )


async def human_era_slice(
    merchant_id: str, thread_id: str, since: datetime
) -> List[TimelineRow]:
    """What she and the teammate said since ``since`` — notes excluded:
    they were never said to her."""
    return await message_accessor.rows_after(
        merchant_id, thread_id, [KIND_INBOUND, KIND_OUTBOUND], since, TURN_ROWS_MAX
    )


async def record_bot_reply(
    merchant_id: str,
    thread_id: str,
    message_id: Optional[str],
    provider_message_id: Optional[str],
    body: Dict[str, Any],
    preview: Optional[str] = None,
) -> Optional[TimelineRow]:
    """Buddy's reply onto the timeline. None when this send is already there."""
    now = datetime.now(timezone.utc)
    row = await message_accessor.insert_outbound(
        None,
        merchant_id,
        thread_id,
        KIND_OUTBOUND,
        AUTHOR_ASSIST,
        None,
        message_id,
        provider_message_id,
        body,
        now,
    )
    if row is not None:
        await thread_accessor.touch_outbound(merchant_id, thread_id, now, preview)
        await wake(merchant_id, thread_id, WAKE_MESSAGE)
    return row


async def mark_bot_cursor(
    merchant_id: str, thread_id: str, answered_upto: datetime
) -> Optional[Thread]:
    return await thread_accessor.mark_bot_cursor(merchant_id, thread_id, answered_upto)


async def thread_by_id(merchant_id: str, thread_id: str) -> Optional[Thread]:
    """The thread as it stands — the responder reads it to decide how a
    session that no longer holds it ended."""
    return await thread_accessor.get_thread(merchant_id, thread_id)
