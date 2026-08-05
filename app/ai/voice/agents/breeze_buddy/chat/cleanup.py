"""Idle chat-session cleanup task.

Marks ACTIVE/IDLE rows whose ``last_activity_at`` is past
``CHAT_SESSION_END_TIMEOUT_SECONDS`` as ENDED. Registered with the global
``BackgroundTaskScheduler`` whose distributed Redis lock keeps the sweep
single-pod-per-tick. UPDATE is idempotent (``WHERE status <> 'ENDED'``), so
duplicate runs from a botched lock acquire are safe.
"""

from datetime import timedelta

from app.ai.voice.agents.breeze_buddy.chat.approvals import (
    terminate_pending_approvals,
)
from app.ai.voice.agents.breeze_buddy.services.conversation_analysis.queue import (
    enqueue_conversation_evaluation,
)
from app.core.config.dynamic import CHAT_SESSION_END_TIMEOUT_SECONDS
from app.core.logger import logger
from app.database.accessor.breeze_buddy.chat_session import (
    end_chat_session,
    list_idle_chat_sessions,
)
from app.schemas.breeze_buddy.chat import ChatEndedReason, ChatSessionStatus
from app.schemas.breeze_buddy.conversation_analysis import ConversationChannel
from app.services.redis.locks import LockAcquireError, RedisLock
from app.utils.common import utcnow

# Same lock key + TTL as the per-turn lock in
# ``app/api/routers/breeze_buddy/chat/handlers.py`` — must stay in sync.
# Inlined (rather than imported) to keep the chat agent tree free of a
# routers/ dependency. Cleanup work itself is sub-second; the TTL exists
# only as a safety net so a crashed pod can't pin an in-flight session
# forever.
_SESSION_LOCK_TTL_SECONDS = 180

# Per-tick processing cap. Higher than the accessor default (100) so that
# a transient backlog from one stalled tick is drained on the next one.
# We deliberately don't loop within a single tick: stale rows we skipped
# (because a turn was mid-flight) would re-appear in the next query and
# spin until a per-tick cap. One batch per tick keeps the math simple
# and the scheduler unblocked.
_SWEEP_BATCH_SIZE = 1_000


def _lock_key(session_id: str) -> str:
    return f"chat:session:{session_id}:lock"


async def end_idle_chat_sessions() -> None:
    """Sweep ACTIVE/IDLE rows past the inactivity threshold; mark ENDED.

    Each session is closed under the same per-session ``RedisLock`` that
    ``POST /message`` and ``POST /end`` take. Without it, this sweeper
    could mark a session ENDED while another pod is mid-turn — that pod
    would happily insert the assistant reply and bump ``last_activity_at``
    against a row this task has already terminated, breaking the chat
    mode "single mutual-exclusion primitive across pods" guarantee.
    A ``LockAcquireError`` here means a turn is in flight, so the session
    isn't actually idle and we skip it; the next tick will catch it.
    """
    idle_after = await CHAT_SESSION_END_TIMEOUT_SECONDS()
    cutoff = utcnow() - timedelta(seconds=idle_after)

    try:
        stale = await list_idle_chat_sessions(
            cutoff=cutoff,
            statuses=[ChatSessionStatus.ACTIVE, ChatSessionStatus.IDLE],
            limit=_SWEEP_BATCH_SIZE,
        )
    except Exception as exc:
        logger.error(f"chat cleanup: list_idle_chat_sessions failed: {exc}")
        return

    if not stale:
        return

    ended = 0
    skipped = 0
    for row in stale:
        session_id = str(row.id)
        lock = RedisLock(_lock_key(session_id), ttl_seconds=_SESSION_LOCK_TTL_SECONDS)
        try:
            await lock.acquire()
        except LockAcquireError:
            skipped += 1
            continue
        try:
            ended_row = await end_chat_session(
                session_id=session_id,
                ended_reason=ChatEndedReason.IDLE_TIMEOUT,
            )
            if ended_row:
                await enqueue_conversation_evaluation(
                    str(ended_row.id),
                    ConversationChannel.CHAT,
                    str(ended_row.template_id),
                )
            ended += 1
            # Terminal sweep: resolve any approvals left PENDING on the now-
            # ENDED session (chat's analog of voice deny_all). Best-effort —
            # a failure here must not undo the end. Runs under the same lock.
            try:
                await terminate_pending_approvals(session_id)
            except Exception as exc:
                logger.warning(
                    f"chat cleanup: terminate approvals failed for {session_id}: {exc}"
                )
        except Exception as exc:
            logger.warning(f"chat cleanup: end_chat_session failed for {row.id}: {exc}")
        finally:
            await lock.release()

    saturated = len(stale) >= _SWEEP_BATCH_SIZE
    logger.info(
        f"chat cleanup: marked {ended}/{len(stale)} idle session(s) ENDED "
        f"({skipped} skipped, threshold={idle_after}s"
        f"{', batch saturated — more will be processed next tick' if saturated else ''})"
    )


__all__ = ["end_idle_chat_sessions"]
