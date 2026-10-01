"""Buddy answering on WhatsApp — the responder pod (CRM_ROLE=responder).

One loop, many pods: each pass leases threads whose customer wrote and Buddy
has not answered (conversations' claim, SKIP LOCKED), and answers each in
its own task:

    1. the burst since Buddy last answered (burst.py) — words become ONE
       turn; only-unreadable gets the merchant's non-text message once;
       reactions get nothing
    2. the thread's chat_session for this window (sessions.py), under the
       same per-session lock every chat path takes
    3. run_chat_turn, text only; EVERY assistant message goes out in order
       as WhatsApp text, each part re-checked with bot_may_speak first — a
       teammate taking over mid-turn stops the rest
    4. out of credits: silent — no reply, the turn is left unanswered
    5. the cursor moves past the burst once a turn starts, whatever came of
       it: a turn is answered at most once (a retry would bill and persist
       her message twice)

Every so often the pass also ends the sessions their threads let go of
(sessions.reconcile_sessions) and says it is alive.
"""

import asyncio
import random
import time
from contextlib import aclosing
from datetime import timedelta
from typing import Any, AsyncGenerator, Dict, Optional, cast

from app.ai.voice.agents.breeze_buddy.chat.sse import SSEEvent
from app.ai.voice.agents.breeze_buddy.chat.turn_core import run_chat_turn
from app.ai.voice.agents.breeze_buddy.chat.whatsapp.burst import (
    Burst,
    plan_burst,
    with_earlier,
)
from app.ai.voice.agents.breeze_buddy.chat.whatsapp.format import split, to_whatsapp
from app.ai.voice.agents.breeze_buddy.chat.whatsapp.sessions import (
    CHANNEL,
    reconcile_sessions,
    session_for,
)
from app.core.config.static import (
    CRM_RESPONDER_BATCH,
    CRM_RESPONDER_INTERVAL,
    CRM_RESPONDER_RECONCILE_SECONDS,
    CRM_RESPONDER_SETTLE_SECONDS,
    CRM_WORKER_HEARTBEAT,
)
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
    bot_may_speak,
    claim_bot_work,
    human_era_slice,
    mark_bot_cursor,
    pending_inbound,
    record_bot_reply,
)
from app.services.redis.locks import (
    SESSION_LOCK_TTL_SECONDS,
    LockAcquireError,
    RedisLock,
)

NAME = "responder"
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


def _lock(session_id: str) -> RedisLock:
    return RedisLock(
        f"chat:session:{session_id}:lock", ttl_seconds=SESSION_LOCK_TTL_SECONDS
    )


async def answer(work: BotWork) -> str:
    """Answer one leased thread; returns what was done (for the logs)."""
    update_log_context(
        component=NAME, merchant_id=work.merchant_id, thread_id=work.thread_id
    )
    rows = await pending_inbound(work.merchant_id, work.thread_id)
    burst = plan_burst(rows)
    if burst is None:
        await mark_bot_cursor(work.merchant_id, work.thread_id, work.last_inbound_at)
        return NOTHING
    if not await bot_may_speak(work.merchant_id, work.thread_id):
        # Taken, waited on, or no longer Buddy's number since the claim:
        # these messages are not Buddy's to answer, now or later.
        await _done(work, burst)
        return NOT_BUDDYS
    if burst.text is None:
        if burst.non_text_only:
            await _send_non_text(work, burst)
        await _done(work, burst)
        return NON_TEXT if burst.non_text_only else NOTHING

    found = await session_for(work)
    if found is None:
        await _done(work, burst)
        return NO_AGENT
    session_id, created = found
    update_log_context(session_id=session_id)
    content = burst.text
    if created:
        content = await _with_earlier(work, burst, rows, content)

    lock = _lock(session_id)
    try:
        await lock.acquire()
    except LockAcquireError:
        # A turn for this session is still running (a lease that ran out
        # under a slow turn): leave the burst for the next lease.
        logger.warning(f"{NAME}: session {session_id} busy; retrying later")
        return BUSY
    try:
        await _turn(work, session_id, content)
    finally:
        try:
            await _done(work, burst)
        finally:
            await lock.release()
    return DONE


async def _done(work: BotWork, burst: Burst) -> None:
    await mark_bot_cursor(work.merchant_id, work.thread_id, burst.upto)


async def _with_earlier(work: BotWork, burst: Burst, rows: list, text: str) -> str:
    profile = conversation_profile(CHANNEL)
    hours = profile.window_hours if profile is not None else 24
    earlier = await human_era_slice(
        work.merchant_id, work.thread_id, burst.upto - timedelta(hours=hours)
    )
    return with_earlier(text, earlier, {row.id for row in rows})


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
    """One assistant message, as WhatsApp text in order. False stops the
    turn: Buddy may no longer speak, or the number refused the send."""
    idx = data.get("idx")
    parts = split(to_whatsapp(str(data.get("content") or "")))
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


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------


async def _answer_safely(work: BotWork) -> None:
    clear_log_context()
    try:
        await answer(work)
    except Exception as e:  # noqa: BLE001 — one thread never stops the pass
        logger.opt(exception=e).error(f"{NAME}: thread {work.thread_id} failed")


async def run_responder(stop_event: asyncio.Event) -> None:
    """Claim, answer the batch concurrently (one task per thread — turns are
    slow and independent), repeat; reconcile sessions and beat on a timer."""
    backoff = CRM_RESPONDER_INTERVAL
    answered = 0
    last_beat = last_reconcile = time.monotonic()
    while not stop_event.is_set():
        clear_log_context()
        now = time.monotonic()
        if now - last_beat >= CRM_WORKER_HEARTBEAT:
            logger.bind(worker=NAME, rows_since_beat=answered).info(
                f"{NAME}: alive, {answered} threads since last heartbeat"
            )
            answered, last_beat = 0, now
        if now - last_reconcile >= CRM_RESPONDER_RECONCILE_SECONDS:
            last_reconcile = now
            try:
                ended = await reconcile_sessions()
                if ended:
                    logger.info(f"{NAME}: ended {ended} released session(s)")
            except Exception as e:  # noqa: BLE001
                logger.bind(worker=NAME).error(f"{NAME}: reconcile failed: {e}")
        try:
            batch = await claim_bot_work(
                [CHANNEL], CRM_RESPONDER_BATCH, CRM_RESPONDER_SETTLE_SECONDS
            )
        except Exception as e:  # noqa: BLE001
            logger.bind(worker=NAME).error(f"{NAME}: claim failed: {e}")
            batch = []
        if not batch:
            await _wait(backoff, stop_event)
            backoff = min(backoff * 2, 5.0)
            continue
        backoff = CRM_RESPONDER_INTERVAL
        answered += len(batch)
        await asyncio.gather(*(_answer_safely(work) for work in batch))


async def _wait(base: float, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(
            stop_event.wait(), timeout=max(0.0, base * random.uniform(0.8, 1.2))
        )
    except asyncio.TimeoutError:
        pass


_task: Optional[asyncio.Task] = None
_stop_event: Optional[asyncio.Event] = None


async def start_responder() -> None:
    global _task, _stop_event
    if _task is not None:
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(run_responder(_stop_event), name=f"crm-{NAME}")


async def stop_responder(timeout: float = 30.0) -> None:
    """Let in-flight turns finish (a turn cut short leaves her unanswered),
    then cancel what is left."""
    global _task, _stop_event
    if _task is None or _stop_event is None:
        return
    _stop_event.set()
    try:
        await asyncio.wait_for(_task, timeout=timeout)
    except asyncio.TimeoutError:
        _task.cancel()
    _task = None
    _stop_event = None
