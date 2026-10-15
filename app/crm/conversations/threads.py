"""A teammate's actions on a thread: take over, assign, hand back, resolve,
note, mark read — and the read side the Inbox lists from.

Every action is fail-closed on permission: a read-only session acts on
nothing, human handoff switched off (D15) refuses taking a thread at all,
and only the teammate holding a thread hands it back.
"""

import base64
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app.crm.connectivity.contracts import (
    ConversationSettings,
    conversation_profile,
    conversation_settings,
    message_ticks,
)
from app.crm.conversations.access import Actor
from app.crm.conversations.db import DbTxn, atomically
from app.crm.conversations.db.accessors import (
    handoff as handoff_accessor,
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.errors import NotAllowed, ThreadConflict, ThreadNotFound
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import (
    Handoff,
    Thread,
    ThreadDetail,
    ThreadPage,
    ThreadRead,
    Ticks,
    TimelineItem,
    TimelinePage,
    TimelineRow,
    Window,
)
from app.crm.conversations.state import held_by
from app.crm.conversations.status import (
    AUTHOR_TEAMMATE,
    CHANNEL_WIDGET,
    KIND_NOTE,
    OUTCOME_HANDED_BACK,
    OUTCOME_RESOLVED,
    VIEWS,
    WAKE_MESSAGE,
    WAKE_READ,
    WAKE_STATE,
)
from app.crm.conversations.window import closes_at, is_open

PAGE_MAX = 100


# ---------------------------------------------------------------------------
# gather
# ---------------------------------------------------------------------------


async def thread_or_404(merchant_id: str, thread_id: str) -> Thread:
    thread = await thread_accessor.get_thread(merchant_id, thread_id)
    if thread is None:
        raise ThreadNotFound("no such conversation")
    return thread


async def settings_for(thread: Thread) -> ConversationSettings:
    """Buddy's settings as they apply to this thread. A widget thread exists
    only because its agent handed off, which the widget allows only with
    handoff on — so it reads as on."""
    if thread.channel == CHANNEL_WIDGET:
        return ConversationSettings(human_handoff=True)
    return await conversation_settings(
        thread.merchant_id, thread.channel, thread.binding_id
    )


def window_of(thread: Thread, now: datetime) -> Window:
    profile = conversation_profile(thread.channel)
    if profile is None:
        # A live channel (the widget) has no window: it is open while the
        # session is.
        return Window(open=thread.resolved_at is None, closes_at=None)
    return Window(
        open=is_open(thread.last_inbound_at, profile.window_hours, now),
        closes_at=closes_at(thread.last_inbound_at, profile.window_hours),
    )


def _read(
    thread: Thread, handoff: Optional[Handoff], sla: int, now: datetime
) -> ThreadRead:
    return ThreadRead(
        id=thread.id,
        channel=thread.channel,
        customer_id=thread.customer_id,
        address=thread.address,
        binding_id=thread.binding_id,
        held_by=held_by(thread, handoff, sla, now),
        assignee_user_id=thread.assignee_user_id,
        unread=thread.unread,
        preview=thread.preview,
        last_message_at=thread.last_message_at,
        window=window_of(thread, now),
        handoff=handoff,
    )


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


def _encode(at: datetime, id_: str) -> str:
    return base64.urlsafe_b64encode(f"{at.isoformat()}|{id_}".encode()).decode()


def _decode(cursor: Optional[str]) -> Optional[Tuple[datetime, str]]:
    if not cursor:
        return None
    try:
        at, id_ = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return datetime.fromisoformat(at), id_
    except (ValueError, UnicodeDecodeError) as e:
        raise ThreadConflict("that page cursor is not one we issued") from e


async def list_threads(
    merchant_id: str,
    actor: Actor,
    view: str,
    channel: Optional[str],
    search: Optional[str],
    cursor: Optional[str],
    limit: int,
) -> ThreadPage:
    """One page of a view, newest first, with every open view's count."""
    if view not in VIEWS:
        raise ThreadNotFound(f"no such view '{view}'")
    limit = max(1, min(limit, PAGE_MAX))
    threads = await thread_accessor.list_threads(
        merchant_id,
        view,
        actor.user_id,
        channel,
        search or None,
        _decode(cursor),
        limit,
    )
    handoffs = await handoff_accessor.open_for_threads(
        merchant_id, [t.id for t in threads]
    )
    counts = await thread_accessor.view_counts(merchant_id, actor.user_id, channel)
    now = datetime.now(timezone.utc)
    slas = await _claim_slas(threads, handoffs)
    rows = [_read(t, handoffs.get(t.id), slas.get(t.id, 0), now) for t in threads]
    last = threads[-1] if len(threads) == limit else None
    return ThreadPage(
        threads=rows,
        next_cursor=_encode(last.last_message_at, last.id) if last else None,
        counts=counts,
    )


async def _claim_slas(
    threads: List[Thread], handoffs: Dict[str, Handoff]
) -> Dict[str, int]:
    """The claim SLA of each thread waiting on a handoff, from the settings
    on its own channel and binding — the read thread_detail makes, so the
    list and the open thread agree. One read per binding on the page."""
    by_binding: Dict[Tuple[str, Optional[str]], int] = {}
    slas: Dict[str, int] = {}
    for thread in threads:
        if thread.id not in handoffs:
            continue
        key = (thread.channel, thread.binding_id)
        if key not in by_binding:
            by_binding[key] = (await settings_for(thread)).claim_sla_minutes
        slas[thread.id] = by_binding[key]
    return slas


async def thread_detail(merchant_id: str, thread_id: str) -> ThreadDetail:
    thread = await thread_or_404(merchant_id, thread_id)
    handoff = await handoff_accessor.open_for_thread(merchant_id, thread_id)
    settings = await settings_for(thread)
    read = _read(
        thread, handoff, settings.claim_sla_minutes, datetime.now(timezone.utc)
    )
    return ThreadDetail(
        **read.model_dump(),
        resolved_at=thread.resolved_at,
        bot_session_id=thread.bot_session_id,
        human_handoff=settings.human_handoff,
        assignment_trail=thread.assignment_trail,
    )


async def timeline(
    merchant_id: str, thread_id: str, before: Optional[str], limit: int
) -> TimelinePage:
    """One page of the thread, newest first, with each send's ticks joined
    from the manifest at read."""
    await thread_or_404(merchant_id, thread_id)
    limit = max(1, min(limit, PAGE_MAX))
    rows = await message_accessor.timeline(
        merchant_id, thread_id, _decode(before), limit
    )
    ticks = await message_ticks(
        merchant_id, [r.message_id for r in rows if r.message_id]
    )
    items = [
        TimelineItem(
            **row.model_dump(),
            ticks=(
                Ticks(**ticks[row.message_id].model_dump(exclude={"id"}))
                if row.message_id and row.message_id in ticks
                else None
            ),
        )
        for row in rows
    ]
    last = rows[-1] if len(rows) == limit else None
    return TimelinePage(
        items=items, next_before=_encode(last.occurred_at, last.id) if last else None
    )


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


def _act(actor: Actor) -> None:
    if actor.read_only:
        raise NotAllowed("this session is read-only — sign in as a teammate to act")


def _trail(actor: Actor, user_id: str, action: str) -> Dict[str, str]:
    return {
        "user_id": user_id,
        "by": actor.user_id,
        "action": action,
        "at": datetime.now(timezone.utc).isoformat(),
    }


async def take_over(merchant_id: str, thread_id: str, actor: Actor) -> Thread:
    """Take the thread: the first teammate wins (compare-and-set), Buddy lets
    go, and the open handoff is claimed."""
    _act(actor)
    thread = await thread_or_404(merchant_id, thread_id)
    if not (await settings_for(thread)).human_handoff:
        raise NotAllowed("human handoff is off for this connection")
    taken = await atomically(
        _take_over_in_txn,
        merchant_id,
        thread_id,
        actor.user_id,
        _trail(actor, actor.user_id, "take_over"),
    )
    if taken is None:
        raise ThreadConflict(
            "someone else is handling this conversation, or it is resolved"
        )
    return taken


async def _take_over_in_txn(
    txn: DbTxn, merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, str]
) -> Optional[Thread]:
    """ATOMIC: the assignee and the handoff's claim land together — a thread
    never shows a teammate while its handoff still waits for one."""
    thread = await thread_accessor.take_over(
        txn, merchant_id, thread_id, user_id, trail
    )
    if thread is None:
        return None
    await handoff_accessor.claim(txn, merchant_id, thread_id, user_id)
    await wake(merchant_id, thread_id, WAKE_STATE, txn)
    return thread


async def assign(
    merchant_id: str, thread_id: str, user_id: str, actor: Actor
) -> Thread:
    """A manager gives the thread to a teammate (D19)."""
    _act(actor)
    if not actor.manager:
        raise NotAllowed("only merchant admins can assign conversations")
    thread = await thread_or_404(merchant_id, thread_id)
    if not (await settings_for(thread)).human_handoff:
        raise NotAllowed("human handoff is off for this connection")
    assigned = await atomically(
        _assign_in_txn,
        merchant_id,
        thread_id,
        user_id,
        _trail(actor, user_id, "assign"),
    )
    if assigned is None:
        raise ThreadConflict("this conversation is resolved")
    return assigned


async def _assign_in_txn(
    txn: DbTxn, merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, str]
) -> Optional[Thread]:
    """ATOMIC: same fate as a take-over — assignee and handoff claim."""
    thread = await thread_accessor.assign(txn, merchant_id, thread_id, user_id, trail)
    if thread is None:
        return None
    await handoff_accessor.claim(txn, merchant_id, thread_id, user_id)
    await wake(merchant_id, thread_id, WAKE_STATE, txn)
    return thread


async def hand_back(merchant_id: str, thread_id: str, actor: Actor) -> Thread:
    """Back to Buddy, from the teammate holding it. Buddy answers what she
    says next, with the human-era slice as context (PR 3)."""
    _act(actor)
    thread = await thread_or_404(merchant_id, thread_id)
    agent_id = (await settings_for(thread)).default_agent_id
    back = await atomically(
        _hand_back_in_txn,
        merchant_id,
        thread_id,
        actor.user_id,
        agent_id,
        _trail(actor, "", "hand_back"),
    )
    if back is None:
        raise ThreadConflict(
            "only the teammate handling this conversation can hand it back"
        )
    return back


async def _hand_back_in_txn(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    user_id: str,
    agent_id: Optional[str],
    trail: Dict[str, str],
) -> Optional[Thread]:
    """ATOMIC: the teammate lets go and the handoff closes together."""
    thread = await thread_accessor.hand_back(
        txn, merchant_id, thread_id, user_id, agent_id, trail
    )
    if thread is None:
        return None
    await handoff_accessor.close(
        txn, merchant_id, thread_id, OUTCOME_HANDED_BACK, user_id
    )
    await wake(merchant_id, thread_id, WAKE_STATE, txn)
    return thread


async def resolve(merchant_id: str, thread_id: str, actor: Actor) -> Thread:
    """Done. Her next message reopens the thread fresh."""
    _act(actor)
    await thread_or_404(merchant_id, thread_id)
    resolved = await atomically(
        resolve_thread_in_txn, merchant_id, thread_id, OUTCOME_RESOLVED, actor.user_id
    )
    if resolved is None:
        raise ThreadConflict("this conversation is already resolved")
    return resolved


async def resolve_thread_in_txn(
    txn: DbTxn,
    merchant_id: str,
    thread_id: str,
    outcome: str,
    closed_by: Optional[str],
    last_inbound_at: Optional[datetime] = None,
) -> Optional[Thread]:
    """ATOMIC: the thread resolves and its open handoff closes together —
    never a resolved thread still asking for a person. A sweep passes the
    ``last_inbound_at`` it read: if she wrote since, nothing changes."""
    thread = await thread_accessor.resolve(txn, merchant_id, thread_id, last_inbound_at)
    if thread is None:
        return None
    await handoff_accessor.close(txn, merchant_id, thread_id, outcome, closed_by)
    await wake(merchant_id, thread_id, WAKE_STATE, txn)
    return thread


async def mark_read(merchant_id: str, thread_id: str, actor: Actor) -> Thread:
    _act(actor)
    thread = await thread_accessor.mark_read(merchant_id, thread_id)
    if thread is None:
        raise ThreadNotFound("no such conversation")
    await wake(merchant_id, thread_id, WAKE_READ)
    return thread


async def add_note(
    merchant_id: str, thread_id: str, text: str, actor: Actor
) -> TimelineRow:
    """A note for the team — never sent to the customer."""
    _act(actor)
    await thread_or_404(merchant_id, thread_id)
    row = await message_accessor.insert_outbound(
        None,
        merchant_id,
        thread_id,
        KIND_NOTE,
        AUTHOR_TEAMMATE,
        actor.user_id,
        None,
        None,
        {"text": text},
        datetime.now(timezone.utc),
    )
    if row is None:
        raise RuntimeError("note insert returned no row")
    await wake(merchant_id, thread_id, WAKE_MESSAGE)
    return row
