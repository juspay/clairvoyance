"""The inbox sweeps — run every CRM_INBOX_SWEEP_SECONDS on each walker pod
(workers.py). Each is idempotent, so two pods sweeping at once at worst do
a step twice that the second time changes nothing.

    closing     a thread's window is about to shut (R5): Buddy, or a teammate
                silent for an hour (D9), sends the closing message once
                (the send's dedupe key is the window), and the thread
                resolves. A window that already shut resolves quietly. A
                thread whose binding is no longer Buddy's resolves quietly too
                — the backstop for a lost "Buddy moved" letter (R7).
    lapses      an unclaimed handoff past the claim SLA closes and Buddy
                takes the thread back (D5).
    retention   resolved threads older than the retention window are
                deleted with their timeline and handoffs (D22).
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from app.core.config.static import CRM_INBOX_RETENTION_DAYS
from app.core.logger import logger
from app.crm.connectivity.contracts import (
    CLAIM_SLA_MINUTES_RANGE,
    CLOSING_LEAD_MINUTES_RANGE,
    ConversationSettings,
    TextBody,
    conversation_settings,
    get_conversation_channel_limits,
    get_current_buddy_binding,
    get_supported_channels_for_conversations,
    send_reply,
)
from app.crm.conversations.db import atomically
from app.crm.conversations.db.accessors import (
    message as message_accessor,
    thread as thread_accessor,
)
from app.crm.conversations.handoffs import close_handoff_if_lapsed
from app.crm.conversations.project import build_preview
from app.crm.conversations.realtime import wake
from app.crm.conversations.schemas import Thread
from app.crm.conversations.state import held_by
from app.crm.conversations.status import (
    AUTHOR_ASSIST,
    AUTHOR_TEAMMATE,
    HELD_BY_BUDDY,
    HELD_BY_TEAMMATE,
    HELD_WAITING,
    KIND_OUTBOUND,
    OUTCOME_BINDING_CHANGED,
    OUTCOME_EXPIRED,
    SERVICE_PURPOSE,
    SOURCE_AGENT,
    SOURCE_HUMAN,
    WAKE_MESSAGE,
)
from app.crm.conversations.threads import resolve_thread_in_txn
from app.crm.conversations.window import closes_at, is_closing_message_due

#: The longest closing lead Buddy's settings allow — the candidate query
#: narrows by it.
MAX_CLOSING_LEAD_MINUTES = CLOSING_LEAD_MINUTES_RANGE[1]
#: A teammate quieter than this gets the closing message sent for them (D9).
TEAMMATE_QUIET_PERIOD = timedelta(hours=1)
#: The shortest claim SLA Buddy's settings allow — lapse candidates narrow
#: by it before each merchant's own SLA decides.
MIN_CLAIM_SLA_SECONDS = CLAIM_SLA_MINUTES_RANGE[0] * 60
#: Pages one sweep walks at most (each ``batch`` rows): enough to pass every
#: thread waiting near its window's end, bounded so one sweep cannot run on.
MAX_SWEEP_PAGES = 20

ACTION_WAIT = "wait"
ACTION_CLOSE = "close"
ACTION_EXPIRE = "expire"
ACTION_MOVED = "moved"


def decide_closing_action(
    thread: Thread,
    buddy_binding_id: Optional[str],
    held: str,
    lead_minutes: int,
    window_hours: int,
    teammate_last_reply: Optional[datetime],
    now: datetime,
) -> str:
    """PURE: what the closing sweep does with one open thread."""
    if thread.binding_id != buddy_binding_id:
        return ACTION_MOVED
    end = closes_at(thread.last_inbound_at, window_hours)
    if end is None:
        return ACTION_WAIT
    if now >= end:
        return ACTION_EXPIRE
    if not is_closing_message_due(
        thread.last_inbound_at, window_hours, lead_minutes, now
    ):
        return ACTION_WAIT
    if held in (HELD_BY_BUDDY, HELD_WAITING):
        return ACTION_CLOSE
    if held == HELD_BY_TEAMMATE and (
        teammate_last_reply is None
        or now - teammate_last_reply >= TEAMMATE_QUIET_PERIOD
    ):
        return ACTION_CLOSE
    # Nobody answering: nobody to say goodbye for — it expires quietly.
    return ACTION_WAIT


async def sweep_closing_windows(batch: int) -> Dict[str, int]:
    done = {ACTION_CLOSE: 0, ACTION_EXPIRE: 0, ACTION_MOVED: 0}
    now = datetime.now(timezone.utc)
    # Every channel whose threads have a reply window to close.
    for channel in get_supported_channels_for_conversations():
        profile = get_conversation_channel_limits(channel)
        if profile is None:
            continue
        older_than = profile.window_hours * 3600 - MAX_CLOSING_LEAD_MINUTES * 60
        buddies: Dict[str, Optional[str]] = {}
        settings: Dict[str, ConversationSettings] = {}
        after: Optional[Tuple[datetime, str]] = None
        for _page in range(MAX_SWEEP_PAGES):
            threads = await thread_accessor.get_threads_near_window_close(
                channel, older_than, after, batch
            )
            for thread in threads:
                try:
                    action = await _close_thread_window(
                        thread, profile.window_hours, buddies, settings, now
                    )
                except Exception as e:  # noqa: BLE001 — one thread never stops it
                    logger.opt(exception=e).error(
                        f"closing sweep: thread {thread.id} failed"
                    )
                    continue
                if action in done:
                    done[action] += 1
            last = threads[-1] if len(threads) == batch else None
            if last is None or last.last_inbound_at is None:
                break
            after = (last.last_inbound_at, last.id)
    return done


async def _close_thread_window(
    thread: Thread,
    window_hours: int,
    buddies: Dict[str, Optional[str]],
    settings: Dict[str, ConversationSettings],
    now: datetime,
) -> str:
    merchant_id = thread.merchant_id
    if merchant_id not in buddies:
        buddy = await get_current_buddy_binding(merchant_id, thread.channel)
        buddies[merchant_id] = buddy.id if buddy is not None else None
        settings[merchant_id] = await conversation_settings(
            merchant_id, thread.channel, buddies[merchant_id]
        )
    config = settings[merchant_id]
    held = held_by(thread, config.claim_sla_minutes, now)
    teammate_last = (
        await message_accessor.get_last_message_time_by_author(
            merchant_id, thread.id, KIND_OUTBOUND, AUTHOR_TEAMMATE
        )
        if held == HELD_BY_TEAMMATE
        else None
    )
    action = decide_closing_action(
        thread,
        buddies[merchant_id],
        held,
        config.closing_lead_minutes,
        window_hours,
        teammate_last,
        now,
    )
    if action == ACTION_WAIT:
        return action
    if action == ACTION_CLOSE:
        await _send_closing_message(thread, config.closing_message, held, now)
    outcome = OUTCOME_BINDING_CHANGED if action == ACTION_MOVED else OUTCOME_EXPIRED
    # Resolved only as read: a page is worked one thread at a time (a close
    # is a live send), so she may have written since — that thread stays open.
    resolved = await atomically(
        resolve_thread_in_txn,
        merchant_id,
        thread.id,
        outcome,
        None,
        thread.last_inbound_at,
    )
    return action if resolved is not None else ACTION_WAIT


async def _send_closing_message(
    thread: Thread, text: str, held: str, now: datetime
) -> None:
    """The closing message, once per window: its dedupe key names the
    window, so a second sweep (or pod) finds the first send instead."""
    if (
        thread.customer_id is None
        or thread.address is None
        or thread.last_inbound_at is None
    ):
        return
    teammate = held == HELD_BY_TEAMMATE
    result = await send_reply(
        merchant_id=thread.merchant_id,
        customer_id=thread.customer_id,
        channel=thread.channel,
        address=thread.address,
        body=TextBody(text=text),
        source_kind=SOURCE_HUMAN if teammate else SOURCE_AGENT,
        source_id=None,
        purpose_key=SERVICE_PURPOSE,
        dedupe_key=f"closing:{thread.id}:{int(thread.last_inbound_at.timestamp())}",
        binding_id=thread.binding_id,
    )
    if result.duplicate:
        return
    row = await message_accessor.insert_outbound(
        None,
        thread.merchant_id,
        thread.id,
        KIND_OUTBOUND,
        AUTHOR_TEAMMATE if teammate else AUTHOR_ASSIST,
        thread.assignee_user_id if teammate else None,
        result.message_id,
        result.provider_message_id,
        {"type": "text", "text": text, "closing": True},
        now,
    )
    if row is not None:
        # The list row moves like any reply's: the Inbox shows the closing
        # message as the thread's last word, and the stream wakes for it.
        await thread_accessor.touch_outbound(
            thread.merchant_id, thread.id, now, build_preview(text)
        )
        await wake(thread.merchant_id, thread.id, WAKE_MESSAGE)


async def sweep_lapsed_handoffs(batch: int) -> int:
    now = datetime.now(timezone.utc)
    lapsed = 0
    after: Optional[Tuple[datetime, str]] = None
    for _page in range(MAX_SWEEP_PAGES):
        waiting = await thread_accessor.get_threads_waiting_longer_than(
            MIN_CLAIM_SLA_SECONDS, after, batch
        )
        for thread in waiting:
            try:
                lapsed += int(await close_handoff_if_lapsed(thread, now))
            except Exception as e:  # noqa: BLE001 — one handoff never stops it
                logger.opt(exception=e).error(
                    f"lapse sweep: handoff on thread {thread.id} failed"
                )
        if len(waiting) < batch:
            break
        last = waiting[-1]
        if last.handoff_requested_at is None:
            break
        after = (last.handoff_requested_at, last.id)
    return lapsed


async def sweep_threads_past_retention(batch: int) -> int:
    return await thread_accessor.delete_threads_past_retention(
        CRM_INBOX_RETENTION_DAYS, batch
    )


async def run_sweeps(batch: int) -> Dict[str, int]:
    """Every sweep, each fenced: one failing never stops the others."""
    counts: Dict[str, int] = {}
    for name, sweep in (
        ("closing", sweep_closing_windows),
        ("lapses", sweep_lapsed_handoffs),
        ("retention", sweep_threads_past_retention),
    ):
        try:
            result = await sweep(batch)
        except Exception as e:  # noqa: BLE001
            logger.opt(exception=e).error(f"inbox sweep {name} failed")
            continue
        if isinstance(result, dict):
            counts.update({f"{name}.{k}": v for k, v in result.items()})
        else:
            counts[name] = result
    if any(counts.values()):
        logger.bind(**{k.replace(".", "_"): v for k, v in counts.items()}).info(
            "inbox sweeps: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v)
        )
    return counts
