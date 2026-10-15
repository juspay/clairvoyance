"""What Buddy's turns (PR 3, buddy-side, on the API pods) work through —
the thread side of a turn. A turn never reads crm tables; it asks here.

    bot_work            the thread as a turn answers it: open, an agent
                        set, nobody holding it, and she wrote since Buddy
                        last answered — else None
    bot_may_speak       re-checked before EVERY send: nobody took the
                        thread, no person is being waited for (bar the
                        handoff this very session just asked for — its
                        waiting message still goes), and it is still on
                        Buddy's binding
    set_bot_session     the chat_session answering this window
    pending_inbound     what she said since Buddy last answered
    human_era_slice     what was said while a teammate held it (context
                        for the first turn after a hand back)
    record_bot_reply    Buddy's reply onto the timeline (read-your-writes)
    mark_bot_cursor     the turn answered up to here
    thread_by_id        the thread as it stands (re-read before a turn)
    threads_by_ids      many at once (ending sessions whose thread let go)
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.crm.connectivity.contracts import buddy_binding
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import BotWork, Thread, TimelineRow
from app.crm.conversations.state import bot_answers, held_by
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
    """PURE: the thread as a turn answers it, or None when it isn't Buddy's
    to answer here (state.bot_answers). A widget thread is answered by the
    widget's own route, never here."""
    if thread.channel == CHANNEL_WIDGET or not bot_answers(thread):
        return None
    if thread.bot_template_id is None:
        return None  # ruled out by bot_answers; spelled out for the types
    return BotWork(
        thread_id=thread.id,
        merchant_id=thread.merchant_id,
        channel=thread.channel,
        customer_id=thread.customer_id,
        address=thread.address,
        binding_id=thread.binding_id,
        agent_id=thread.bot_template_id,
        session_id=thread.bot_session_id,
    )


async def owes_reply(thread: Thread) -> bool:
    """Buddy has her messages to answer: the thread is Buddy's to
    answer (state.bot_answers) and she wrote past Buddy's cursor."""
    if not bot_answers(thread):
        return False
    unanswered = await message_accessor.rows_after(
        thread.merchant_id, thread.id, [KIND_INBOUND], thread.bot_cursor_at, 1
    )
    return bool(unanswered)


async def bot_work(
    merchant_id: str, thread_id: str, unanswered_only: bool = True
) -> Optional[BotWork]:
    """The thread as Buddy's turn answers it (see _work), read fresh — None
    unless she wrote since Buddy last answered. ``unanswered_only=False``:
    whenever it is Buddy's to answer (a turn that tells Buddy why it has the
    thread back runs with or without her messages)."""
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return None
    work = _work(thread)
    if work is None or not unanswered_only:
        return work
    return work if await owes_reply(thread) else None


async def bot_may_speak(
    merchant_id: str, thread_id: str, session_id: Optional[str] = None
) -> bool:
    """Fail closed: anything but "Buddy holds it, on Buddy's binding" is no —
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
    buddy = await buddy_binding(merchant_id, thread.channel)
    return buddy is not None and buddy.id == thread.binding_id


async def set_bot_session(
    merchant_id: str, thread_id: str, session_id: str
) -> Optional[Thread]:
    return await thread_accessor.set_bot_session(merchant_id, thread_id, session_id)


async def pending_inbound(merchant_id: str, thread_id: str) -> List[TimelineRow]:
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        return []
    return await message_accessor.rows_after(
        merchant_id, thread_id, [KIND_INBOUND], thread.bot_cursor_at, TURN_ROWS_MAX
    )


async def human_era_slice(
    merchant_id: str, thread_id: str, since: datetime, before: datetime
) -> List[TimelineRow]:
    """The latest of what she and the team said between ``since`` and
    ``before`` (at most TURN_ROWS_MAX rows, oldest first) — notes excluded:
    they were never said to her."""
    return await message_accessor.rows_before(
        merchant_id,
        thread_id,
        [KIND_INBOUND, KIND_OUTBOUND],
        since,
        before,
        TURN_ROWS_MAX,
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
    """Answered up to ``answered_upto``: what she wrote before it is never
    answered again. Forward only."""
    return await thread_accessor.mark_bot_cursor(merchant_id, thread_id, answered_upto)


async def thread_by_id(merchant_id: str, thread_id: str) -> Optional[Thread]:
    """The thread as it stands — Buddy's answer re-reads it under the session
    lock before a turn."""
    return await thread_accessor.get_thread(merchant_id, thread_id)


async def threads_by_ids(merchant_id: str, thread_ids: List[str]) -> Dict[str, Thread]:
    """One merchant's threads as they stand, by id (a deleted one is absent)
    — the idle sweeper reads a page of sessions' threads at once to decide how
    the sessions that no longer hold them ended."""
    if not thread_ids:
        return {}
    threads = await thread_accessor.threads_by_ids(merchant_id, thread_ids)
    return {thread.id: thread for thread in threads}
