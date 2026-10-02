"""One chat_session per stretch of Buddy holding a WhatsApp thread, and how
it ended.

A thread hands its agent a session (``channel='whatsapp'``, its thread in
``metadata.conversation_id``) the first time Buddy answers; the
conversations module clears the thread's session when Buddy lets go of it.
The responder then ends the session here with the honest reason — the idle
sweeper never touches a WhatsApp session:

    number_changed   Buddy moved to another number (the thread is not on
                     Buddy's number any more)
    taken_over       a teammate took the thread, or resolved it well before
                     its window ran out
    window_closed    the reply window ran out (the closing sweep resolved it)
"""

from datetime import timedelta
from typing import Dict, Optional, Tuple

from app.ai.voice.agents.breeze_buddy.template.cache import get_template_by_id_cached
from app.core.logger import logger
from app.crm.connectivity.contracts import buddy_number, conversation_profile
from app.crm.conversations.contracts import (
    CHANNEL_WHATSAPP,
    BotWork,
    Thread,
    set_bot_session,
    thread_by_id,
)
from app.database.accessor.breeze_buddy.chat_session import (
    create_chat_session,
    end_chat_session,
    get_chat_session_by_id,
    list_open_sessions_on_channel,
)
from app.schemas.breeze_buddy.chat import ChatEndedReason, ChatSessionStatus
from app.services.redis.locks import (
    SESSION_LOCK_TTL_SECONDS,
    LockAcquireError,
    RedisLock,
)

CHANNEL = CHANNEL_WHATSAPP
#: Open sessions reconciled per pass (the next pass takes the rest).
RECONCILE_BATCH = 200
#: The closing sweep resolves a thread at most this long before its window
#: shuts (connectivity bounds the closing lead at 120 minutes): a resolve
#: earlier than that was a person's.
CLOSING_LEAD_MAX = timedelta(minutes=120)


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
        logger.error(f"whatsapp: agent {work.agent_id} is gone; nothing answers")
        return None
    session = await create_chat_session(
        template_id=work.agent_id,
        reseller_id=template.reseller_id,
        merchant_id=work.merchant_id,
        metadata={"conversation_id": work.thread_id, "template_vars": {}},
        channel=CHANNEL,
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
        return ChatEndedReason.NUMBER_CHANGED
    if thread.resolved_at is None or thread.last_inbound_at is None:
        # Let go while still open: a teammate took it (handing it back
        # later starts a NEW session).
        return ChatEndedReason.TAKEN_OVER
    shuts = thread.last_inbound_at + timedelta(hours=window_hours)
    if thread.resolved_at < shuts - CLOSING_LEAD_MAX:
        return ChatEndedReason.TAKEN_OVER
    return ChatEndedReason.WINDOW_CLOSED


async def reconcile_sessions() -> int:
    """End every open WhatsApp session its thread no longer holds — under
    the session's lock, so a turn in flight finishes first."""
    profile = conversation_profile(CHANNEL)
    hours = profile.window_hours if profile is not None else 24
    buddies: Dict[str, Optional[str]] = {}
    ended = 0
    sessions = await list_open_sessions_on_channel(
        CHANNEL,
        [ChatSessionStatus.ACTIVE, ChatSessionStatus.IDLE],
        RECONCILE_BATCH,
    )
    for session in sessions:
        merchant_id = session.merchant_id
        thread_id = (session.metadata or {}).get("conversation_id")
        if not merchant_id or not thread_id:
            continue
        try:
            thread = await thread_by_id(merchant_id, str(thread_id))
            if merchant_id not in buddies:
                buddy = await buddy_number(merchant_id, CHANNEL)
                buddies[merchant_id] = buddy.id if buddy is not None else None
            reason = end_reason(session.id, thread, buddies[merchant_id], hours)
            if reason is not None and await _end(session.id, reason):
                ended += 1
        except Exception as e:  # noqa: BLE001 — one session never stops the pass
            logger.opt(exception=e).error(f"whatsapp: reconciling {session.id} failed")
    return ended


async def _end(session_id: str, reason: ChatEndedReason) -> bool:
    lock = RedisLock(
        f"chat:session:{session_id}:lock", ttl_seconds=SESSION_LOCK_TTL_SECONDS
    )
    try:
        await lock.acquire()
    except LockAcquireError:
        return False  # a turn is running; the next pass ends it
    try:
        return await end_chat_session(session_id, reason.value) is not None
    finally:
        await lock.release()
