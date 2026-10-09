"""Redis queue for post-conversation evaluations: one job per finished
conversation, running everything its template has on (its topics, and on a
call its custom evals; worker.py).

A call's job is queued once the end-of-call outcome check is done, so the
custom evals' judge sees the call's final outcome: by crm_mirror's finished
tap (``enqueue_conversation_evaluation(..., once_per=...)``), except for a
call on the Daily transport (``queued_at_call_end``), which end_conversation
queues itself. A chat session's job is queued when the session ends."""

from typing import Any, Optional, cast

from app.core.logger import logger
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.redis import get_redis_service

CONVERSATION_EVALUATION_QUEUE = "conversation-evaluation:pending"

#: Every topic-evaluation log line carries this, so one filter finds them all.
TOPICS_LOG_COMPONENT = "buddy.topics"

#: Every agent-evals (custom evals) log line carries this, so one filter finds
#: them all.
EVALS_LOG_COMPONENT = "buddy.evals"

#: A call on the Daily transport: its bot process exits with the call, before
#: a background task is sure to run, and no outcome check runs on it, so
#: end_conversation queues its job (awaited). Every other call's job is queued
#: by crm_mirror's finished tap once the outcome check is done.
_DAILY_MODES = frozenset({"DAILY", "DAILY_TEST", "DAILY_STREAM"})

#: Claims a call's job on the finished tap, which can fire more than once for
#: one call (a lead FINISHED twice): a day outlives any repeat.
_QUEUED_KEY = "conversation-evaluation:queued:{}"
_QUEUED_TTL_SECONDS = 24 * 60 * 60


def queued_at_call_end(execution_mode: Any) -> bool:
    """Whether a call's job is queued by end_conversation (a Daily call)
    rather than by crm_mirror's finished tap."""
    return getattr(execution_mode, "value", execution_mode) in _DAILY_MODES


async def has_enabled_evaluations(template_id: str) -> bool:
    from app.database.accessor.breeze_buddy.evaluation_config import (
        has_enabled_evaluations as check,
    )

    return await check(template_id)


async def _claim(key: str) -> bool:
    """True the first time ``key`` is claimed (within a day)."""
    redis = await get_redis_service()
    return bool(
        await redis.set(_QUEUED_KEY.format(key), "1", nx=True, ex=_QUEUED_TTL_SECONDS)
    )


async def _push(job: ConversationEvaluationJob) -> int:
    """Append ``job`` to the queue; the queue's depth after it."""
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    return await client.rpush(CONVERSATION_EVALUATION_QUEUE, job.model_dump_json())


async def enqueue_conversation_evaluation(
    source_id: str,
    channel: ConversationChannel,
    template_id: str,
    *,
    once_per: Optional[str] = None,
) -> None:
    """A finished conversation's job, when its template has anything on.
    ``once_per`` (the call, on crm_mirror's finished tap) queues it at most
    once for that key. Never raises: a conversation's end never waits on it."""
    try:
        if not await has_enabled_evaluations(template_id):
            return
        if once_per is not None and not await _claim(once_per):
            logger.bind(
                component=TOPICS_LOG_COMPONENT,
                source_id=source_id,
                template_id=template_id,
            ).info(f"Conversation evaluation {source_id} already queued")
            return
        job = ConversationEvaluationJob(
            source_id=source_id,
            channel=channel,
            template_id=template_id,
        )
        queue_depth = await _push(job)
        logger.bind(
            component=TOPICS_LOG_COMPONENT,
            source_id=source_id,
            template_id=template_id,
            channel=channel.value,
            queue_depth=queue_depth,
        ).info(f"Topic evaluation {source_id} enqueued")
    except Exception as exc:
        logger.error(
            f"Failed to enqueue conversation evaluation "
            f"{channel}:{source_id}: {exc}"
        )


async def dequeue_conversation_evaluation() -> ConversationEvaluationJob:
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    popped = await client.blpop(
        CONVERSATION_EVALUATION_QUEUE,
        timeout=0,
    )
    return ConversationEvaluationJob.model_validate_json(popped[1])


async def requeue_conversation_evaluation(job: ConversationEvaluationJob) -> None:
    """Put a job back at the head of the queue so it is the next one tried."""
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    await client.lpush(CONVERSATION_EVALUATION_QUEUE, job.model_dump_json())
