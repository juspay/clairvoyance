"""Buddy answering an inbox thread — on the API pods, the way a widget
message is answered (D41).

The projector (and the lapse sweep) ask for it through the answer route once
her message is on a thread Buddy holds; the route starts ``answer_thread``
here and returns at once. Then, under the thread's lock:

    1. the burst since Buddy last answered (burst.py) — words become ONE
       turn; only-unreadable gets the merchant's non-text message once;
       reactions get nothing
    2. the thread's chat_session for this window (sessions.py), under the
       same per-session lock every chat path takes
    3. run_chat_turn, text only; EVERY assistant message goes out in order
       as the thread channel's text (format.py), each part re-checked with
       bot_may_speak first — a teammate taking over mid-turn stops the rest
    4. out of credits: silent — no reply, the turn is left unanswered
    5. the cursor moves past the burst once a turn starts, whatever came of
       it: a burst is answered at most once (a retry would bill and persist
       her message twice); a turn ends before its session lock can expire

A hand back or a lapsed handoff asks with a ``reason``: that request's first
turn opens with why Buddy has the thread back (burst.resume_note), with her
waiting messages if any — told once, in a turn of its own.

What she writes while a turn runs gets one more turn when it ends. Like the
widget, nothing retries a request that never arrives: her next message asks
again, for everything unanswered.
"""

import asyncio
from contextlib import aclosing
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, cast

from app.ai.voice.agents.breeze_buddy.chat.inbox.burst import (
    Burst,
    plan_burst,
    resume_note,
    turn_message,
)
from app.ai.voice.agents.breeze_buddy.chat.inbox.format import render, split
from app.ai.voice.agents.breeze_buddy.chat.inbox.sessions import (
    session_for,
    session_lock,
)
from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.turn_core import run_chat_turn
from app.core.logger import logger
from app.core.logger.context import clear_log_context, update_log_context
from app.crm.connectivity.contracts import (
    TextBody,
    conversation_profile,
    conversation_settings,
    send_session,
)
from app.crm.conversations.contracts import (
    SERVICE_PURPOSE,
    SOURCE_AGENT,
    BotWork,
    TimelineRow,
    bot_may_speak,
    bot_work,
    human_era_slice,
    mark_bot_cursor,
    pending_inbound,
    record_bot_reply,
    thread_by_id,
)
from app.services.redis.locks import (
    SESSION_LOCK_TTL_SECONDS,
    LockAcquireError,
    RedisLock,
)

NAME = "inbox"
#: A turn ends before the session lock it holds can expire (no renewal), so
#: two turns never run on one session at once.
TURN_LIMIT_SECONDS = SESSION_LOCK_TTL_SECONDS - 20
#: The send manifest's word for a send the provider took.
ACCEPTED = "accepted"
#: The chat engine's code for a merchant out of credits.
NO_CREDITS = "insufficient_credits"

DONE = "answered"
NON_TEXT = "non_text"
NOTHING = "nothing"
NOT_BUDDYS = "not_buddys"
NO_AGENT = "no_agent"
BUSY = "busy"
FAILED = "failed"

#: Turns one request runs at most: hers, then one more each time she wrote
#: during the last — a bound, never a loop she could keep spinning.
MAX_TURNS_PER_REQUEST = 5

#: The answers running on this pod, kept so none is collected mid-turn.
_running: Set["asyncio.Task[None]"] = set()


def _thread_lock(thread_id: str) -> RedisLock:
    """One answer per thread at a time, across pods — taken BEFORE the
    session exists, so two quick messages never open two sessions."""
    return RedisLock(
        f"chat:inbox:thread:{thread_id}:lock", ttl_seconds=SESSION_LOCK_TTL_SECONDS
    )


def start_answer(
    merchant_id: str, thread_id: str, reason: Optional[str] = None
) -> None:
    """Answer the thread in the background on this pod; returns at once."""
    task = asyncio.create_task(
        answer_thread(merchant_id, thread_id, reason),
        name=f"{NAME}-answer-{thread_id}",
    )
    _running.add(task)
    task.add_done_callback(_running.discard)


async def answer_thread(
    merchant_id: str, thread_id: str, reason: Optional[str] = None
) -> None:
    """Answer everything she has written that Buddy hasn't, then once more
    if she wrote while it ran. A thread another request is already answering
    is left to it: that one looks again after letting go of the lock.
    ``reason``: Buddy was just given the thread back — the first turn tells
    it why, even with nothing of hers to answer."""
    clear_log_context()
    for _ in range(MAX_TURNS_PER_REQUEST):
        work = await bot_work(merchant_id, thread_id, unanswered_only=reason is None)
        if work is None:
            return
        lock = _thread_lock(thread_id)
        try:
            await lock.acquire()
        except LockAcquireError:
            return  # being answered; that answer looks again when it ends
        try:
            outcome = await answer(work, reason)
        except Exception as e:  # noqa: BLE001 — one thread's failure is logged
            logger.opt(exception=e).error(f"{NAME}: thread {thread_id} failed")
            outcome = FAILED
        finally:
            await lock.release()
        reason = None  # told once; a further turn answers only what she wrote
        logger.bind(outcome=outcome).info(f"{NAME}: thread {thread_id}: {outcome}")
        # Looked at again only now, after letting go: a message that lands
        # while the lock was held is either seen here or answered by the
        # request it sent, never by neither.
        if outcome not in (DONE, NON_TEXT):
            return
    logger.warning(
        f"{NAME}: thread {thread_id} still writing after {MAX_TURNS_PER_REQUEST} turns"
    )


async def answer(work: BotWork, reason: Optional[str] = None) -> str:
    """Answer one thread's burst — and, with ``reason``, tell Buddy why it
    has the thread back; returns what was done (for the logs)."""
    update_log_context(
        component=NAME, merchant_id=work.merchant_id, thread_id=work.thread_id
    )
    rows = await pending_inbound(work.merchant_id, work.thread_id)
    burst = plan_burst(rows)
    if burst is None and reason is None:
        return NOTHING  # answered meanwhile: nothing past the cursor
    if not await bot_may_speak(work.merchant_id, work.thread_id):
        # Taken, waited on, or no longer Buddy's binding: not Buddy's to
        # answer now. The cursor stays — a lapse asks Buddy for them, a take
        # over or hand back moves it, a moved binding's thread is resolved.
        return NOT_BUDDYS
    if burst is not None and burst.text is None:
        if burst.non_text_only:
            await _send_non_text(work, burst)
        await _done(work, burst)
        if reason is None:
            return NON_TEXT if burst.non_text_only else NOTHING
        burst = None  # Buddy is still told; no words of hers to add

    found = await session_for(work)
    if found is None:
        if burst is not None:
            await _done(work, burst)
        return NO_AGENT
    session_id, created = found
    update_log_context(session_id=session_id)
    earlier = await _earlier(work, rows) if created else []
    note = await _note(work, reason) if reason else None

    lock = session_lock(session_id)
    try:
        await lock.acquire()
    except LockAcquireError:
        # Something else holds the session (it is being ended): her
        # messages wait for her next one, as on the widget.
        logger.warning(f"{NAME}: session {session_id} busy; not answered now")
        return BUSY
    try:
        # Answered meanwhile (a turn that ended between our read and our
        # lock): answered once — Buddy is still told, if it was given back.
        if burst is not None and await _answered(work, burst):
            if note is None:
                return NOTHING
            burst = None
        # The cursor moves as the turn starts, so the same messages are never
        # answered twice — whatever the turn does.
        if burst is not None:
            await _done(work, burst)
        content = turn_message(
            burst.text if burst is not None else None,
            earlier,
            {row.id for row in rows},
            note,
        )
        try:
            async with asyncio.timeout(TURN_LIMIT_SECONDS):
                await _turn(work, session_id, content)
        except TimeoutError:
            logger.warning(f"{NAME}: turn cut at {TURN_LIMIT_SECONDS}s")
    finally:
        await lock.release()
    return DONE


async def _done(work: BotWork, burst: Burst) -> None:
    await mark_bot_cursor(work.merchant_id, work.thread_id, burst.upto)


async def _answered(work: BotWork, burst: Burst) -> bool:
    thread = await thread_by_id(work.merchant_id, work.thread_id)
    return thread is None or (
        thread.bot_cursor_at is not None and thread.bot_cursor_at >= burst.upto
    )


async def _earlier(work: BotWork, rows: List[TimelineRow]) -> List[TimelineRow]:
    profile = conversation_profile(work.channel)
    hours = profile.window_hours if profile is not None else 24
    # The newest rows before her burst (before now, when Buddy is only being
    # told it has the thread back) — the teammate's last words before a hand
    # back, not the oldest of the window.
    before = rows[0].occurred_at if rows else datetime.now(timezone.utc)
    since = (rows[-1].occurred_at if rows else before) - timedelta(hours=hours)
    return await human_era_slice(work.merchant_id, work.thread_id, since, before)


async def _note(work: BotWork, reason: str) -> str:
    settings = await conversation_settings(
        work.merchant_id, work.channel, work.binding_id
    )
    return resume_note(reason, settings.claim_sla_minutes)


async def _turn(work: BotWork, session_id: str, content: str) -> None:
    # run_chat_turn is an async generator (typed as its iterator): closing
    # it when Buddy must stop runs its cleanup now, not at GC.
    turn = cast(
        AsyncGenerator[SSEEvent, None],
        run_chat_turn(session_id=session_id, user_content=content),
    )
    async with aclosing(turn) as events:
        async for event in events:
            if event.event == "assistant_message":
                if not await _deliver(work, session_id, event.data):
                    return  # the rest of the turn is not Buddy's to say
            elif event.event == "error":
                code = (event.data or {}).get("code")
                if code == NO_CREDITS:
                    logger.info(f"{NAME}: merchant out of credits; staying silent")
                else:
                    logger.error(f"{NAME}: turn failed: {event.data}")


async def _deliver(work: BotWork, session_id: str, data: Dict[str, Any]) -> bool:
    """One assistant message, as the thread channel's text, in order. False
    stops the turn: Buddy may no longer speak, or the send was refused."""
    profile = conversation_profile(work.channel)
    if profile is None:
        logger.error(f"{NAME}: {work.channel} carries no conversation; unsent")
        return False
    idx = data.get("idx")
    content = render(work.channel, str(data.get("content") or ""))
    parts = split(content, profile.text_max)
    for n, part in enumerate(parts):
        if not await bot_may_speak(work.merchant_id, work.thread_id, session_id):
            logger.info(f"{NAME}: thread let go of Buddy mid-turn; rest unsent")
            return False
        key = f"buddy:{session_id}:{idx if idx is not None else 'x'}:{n}"
        if not await _send(work, part, key):
            return False
    return True


async def _send_non_text(work: BotWork, burst: Burst) -> None:
    settings = await conversation_settings(
        work.merchant_id, work.channel, work.binding_id
    )
    await _send(
        work,
        settings.non_text_message,
        f"buddy:non_text:{work.thread_id}:{burst.last_row_id}",
    )


async def _send(work: BotWork, text: str, dedupe_key: str) -> bool:
    if work.customer_id is None or work.address is None:
        logger.error(f"{NAME}: thread has no reply address")
        return False
    result = await send_session(
        merchant_id=work.merchant_id,
        customer_id=work.customer_id,
        channel=work.channel,
        address=work.address,
        body=TextBody(text=text),
        source_kind=SOURCE_AGENT,
        source_id=work.agent_id,
        purpose_key=SERVICE_PURPOSE,
        dedupe_key=dedupe_key,
        binding_id=work.binding_id,
    )
    if not result.duplicate:
        await record_bot_reply(
            work.merchant_id,
            work.thread_id,
            result.message_id,
            result.provider_message_id,
            {"type": "text", "text": text},
            text,
        )
    if result.status != ACCEPTED:
        logger.warning(
            f"{NAME}: send {result.status} ({result.reason}); rest of turn unsent"
        )
        return False
    return True
