"""One chat_session per stretch of Buddy holding an inbox thread, and how
it ended.

A thread hands its agent a session (``channel`` = the thread's channel, its
thread in ``metadata.conversation_id``) the first time Buddy answers; the
conversations module clears the thread's session when Buddy lets go of it.
The idle sweeper (chat/cleanup.py, which ends widget sessions too) then
ends it here with the honest reason — never for inactivity alone:

    binding_changed  Buddy moved to another binding (the thread is not on
                     Buddy's binding any more)
    taken_over       a teammate took the thread, or resolved it well before
                     its window ran out
    window_closed    the reply window ran out (the closing sweep resolved it)
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from uuid import UUID

from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.core.logger import logger
from app.crm.connectivity.contracts import (
    CLOSING_LEAD_MINUTES_RANGE,
    buddy_binding,
    conversation_channels,
    conversation_profile,
)
from app.crm.conversations.contracts import (
    BotWork,
    Thread,
    set_bot_session,
    threads_by_ids,
)
from app.database.accessor.breeze_buddy.chat_session import (
    create_chat_session,
    end_chat_session,
    get_chat_session_by_id,
    list_open_sessions_on_channel,
)
from app.schemas.breeze_buddy.chat import (
    ChatEndedReason,
    ChatSession,
    ChatSessionStatus,
)
from app.services.redis.locks import (
    SESSION_LOCK_TTL_SECONDS,
    LockAcquireError,
    RedisLock,
)

#: Open sessions read per page, and pages per sweep (per channel): a sweep
#: walks every open thread-bound session (held ones included), so a released
#: session is ended within a sweep however many others are still held.
SWEEP_BATCH = 200
SWEEP_MAX_PAGES = 50
#: The closing sweep resolves a thread at most this long before its window
#: shuts (the longest closing lead Buddy's settings allow): a resolve earlier
#: than that was a person's.
CLOSING_LEAD_MAX = timedelta(minutes=CLOSING_LEAD_MINUTES_RANGE[1])


async def session_for(work: BotWork) -> Optional[Tuple[str, bool]]:
    """(session id, created now) for the thread; None when the agent's
    template is gone (nothing can answer)."""
    if work.session_id:
        session = await get_chat_session_by_id(work.session_id)
        if (
            session is not None
            and session.status != ChatSessionStatus.ENDED
            and session.template_id == work.agent_id
        ):
            return session.id, False
    template = await get_template_by_id_cached(work.agent_id)
    if template is None:
        logger.error(f"inbox: agent {work.agent_id} is gone; nothing answers")
        return None
    session = await create_chat_session(
        template_id=work.agent_id,
        reseller_id=template.reseller_id,
        merchant_id=work.merchant_id,
        metadata={"conversation_id": work.thread_id, "template_vars": {}},
        channel=work.channel,
    )
    if session is None:
        return None
    await set_bot_session(work.merchant_id, work.thread_id, session.id)
    return session.id, True


def end_reason(
    session_id: str,
    thread: Optional[Thread],
    buddy_binding_id: Optional[str],
    window_hours: int,
) -> Optional[ChatEndedReason]:
    """PURE: why a session ended, or None while its thread still holds it."""
    if thread is None:
        return ChatEndedReason.WINDOW_CLOSED  # retention deleted it
    if thread.bot_session_id == session_id:
        return None
    if thread.binding_id != buddy_binding_id:
        return ChatEndedReason.BINDING_CHANGED
    if thread.resolved_at is None or thread.last_inbound_at is None:
        # Let go while still open: a teammate took it (handing it back
        # later starts a NEW session).
        return ChatEndedReason.TAKEN_OVER
    shuts = thread.last_inbound_at + timedelta(hours=window_hours)
    if thread.resolved_at < shuts - CLOSING_LEAD_MAX:
        return ChatEndedReason.TAKEN_OVER
    return ChatEndedReason.WINDOW_CLOSED


async def end_released_sessions() -> int:
    """End every open thread-bound session its thread no longer holds, on
    every channel that carries a conversation — under the session's lock,
    so a turn in flight finishes first. The idle sweeper's other half."""
    ended = 0
    for channel in conversation_channels():
        ended += await _end_released_on(channel)
    return ended


async def _end_released_on(channel: str) -> int:
    profile = conversation_profile(channel)
    hours = profile.window_hours if profile is not None else 24
    # Buddy's binding per merchant, on this channel.
    buddies: Dict[str, Optional[str]] = {}
    ended = 0
    after: Optional[Tuple[datetime, str]] = None
    for _page in range(SWEEP_MAX_PAGES):
        sessions = await list_open_sessions_on_channel(
            channel,
            [ChatSessionStatus.ACTIVE, ChatSessionStatus.IDLE],
            SWEEP_BATCH,
            after,
        )
        ended += await _end_released_page(channel, sessions, buddies, hours)
        last = sessions[-1] if sessions else None
        if len(sessions) < SWEEP_BATCH or last is None:
            break
        if last.last_activity_at is None:  # NOT NULL in the table
            break
        after = (last.last_activity_at, last.id)
    return ended


def _thread_of(session: ChatSession) -> Optional[str]:
    """The thread a session answers, as the thread table spells its id;
    None when it names none (nothing to read — never taken as deleted)."""
    raw = (session.metadata or {}).get("conversation_id")
    try:
        return str(UUID(str(raw))) if raw else None
    except ValueError:
        return None


async def _end_released_page(
    channel: str,
    sessions: List[ChatSession],
    buddies: Dict[str, Optional[str]],
    hours: int,
) -> int:
    # One read per merchant on the page, not one per session.
    by_merchant: Dict[str, List[Tuple[ChatSession, str]]] = {}
    for session in sessions:
        thread_id = _thread_of(session)
        if session.merchant_id and thread_id:
            by_merchant.setdefault(session.merchant_id, []).append((session, thread_id))
    ended = 0
    for merchant_id, held in by_merchant.items():
        try:
            threads = await threads_by_ids(merchant_id, [t for _, t in held])
            if merchant_id not in buddies:
                buddy = await buddy_binding(merchant_id, channel)
                buddies[merchant_id] = buddy.id if buddy is not None else None
        except Exception as e:  # noqa: BLE001 — one merchant never stops the sweep
            logger.opt(exception=e).error(
                f"inbox: ending released {channel} sessions of {merchant_id} failed"
            )
            continue
        for session, thread_id in held:
            reason = end_reason(
                session.id, threads.get(thread_id), buddies[merchant_id], hours
            )
            if reason is None:
                continue
            try:
                if await _end(session.id, reason):
                    ended += 1
            except Exception as e:  # noqa: BLE001 — one session never stops it
                logger.opt(exception=e).error(f"inbox: ending {session.id} failed")
    return ended


def session_lock(session_id: str) -> RedisLock:
    """The per-session lock every chat path takes (the widget's turns, the
    idle sweeper, Buddy's inbox answer): one turn on a session at a time."""
    return RedisLock(
        f"chat:session:{session_id}:lock", ttl_seconds=SESSION_LOCK_TTL_SECONDS
    )


async def _end(session_id: str, reason: ChatEndedReason) -> bool:
    lock = session_lock(session_id)
    try:
        await lock.acquire()
    except LockAcquireError:
        return False  # a turn is running; the next sweep ends it
    try:
        return await end_chat_session(session_id, reason.value) is not None
    finally:
        await lock.release()
