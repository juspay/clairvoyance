"""Redis queue for post-conversation evaluations."""

from typing import Any, cast

from app.core.logger import logger
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.redis import get_redis_service

CONVERSATION_EVALUATION_QUEUE = "conversation-evaluation:pending"

#: Every topic-evaluation log line carries this, so one filter finds them all.
LOG_COMPONENT = "buddy.topics"


async def has_enabled_evaluations(template_id: str) -> bool:
    from app.database.accessor.breeze_buddy.evaluation_config import (
        has_enabled_evaluations as check,
    )

    return await check(template_id)


async def enqueue_conversation_evaluation(
    source_id: str,
    channel: ConversationChannel,
    template_id: str,
) -> None:
    try:
        if not await has_enabled_evaluations(template_id):
            return
        job = ConversationEvaluationJob(
            source_id=source_id,
            channel=channel,
            template_id=template_id,
        )
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        queue_depth = await client.rpush(
            CONVERSATION_EVALUATION_QUEUE, job.model_dump_json()
        )
        logger.bind(
            component=LOG_COMPONENT,
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


# ---------------------------------------------------------------------------
# The agent's own evals (merchant evals): not live yet, kept for when they
# come. Enabling them means:
#   - uncommenting this, ``kind`` on ConversationEvaluationJob, the "evals"
#     dispatch in worker._evaluate, conversation_analysis/custom/agent_evals.py and
#     app/services/evals/custom/batch.py;
#   - calling ``enqueue_call_evaluations`` from crm_mirror's finished tap once
#     the outcome check is done (``checked_outcome``), and moving the voice
#     topics enqueue there from end_conversation, so both jobs see the call's
#     final outcome.
# One queue then carries two kinds of job: a conversation's topics (voice and
# chat, as ever) and an agent's own evals (voice only at first), each
# retried on its own.
# ---------------------------------------------------------------------------
#
# #: Every agent-evals log line carries this, so one filter finds them all.
# EVALS_LOG_COMPONENT = "buddy.evals"
#
#
# def has_customer_turn(transcript: Any) -> bool:
#     """Whether a transcript holds something the customer said: a
#     conversation without one is never evaluated (the tap queues nothing
#     for it)."""
#     return isinstance(transcript, list) and any(
#         isinstance(turn, dict)
#         and turn.get("role") == "user"
#         and str(turn.get("content") or "").strip()
#         for turn in transcript
#     )
#
#
# async def _push(job: ConversationEvaluationJob) -> int:
#     """Append ``job`` to the queue; the queue's depth after it."""
#     redis = await get_redis_service()
#     client: Any = cast(Any, await redis.get_client())
#     return await client.rpush(CONVERSATION_EVALUATION_QUEUE, job.model_dump_json())
#
#
# async def enqueue_call_evaluations(lead_id: str, template_id: str) -> None:
#     """A finished call's post-call jobs, queued once its outcome is final
#     (``crm_mirror``'s finished tap, after the outcome check): its topics, as
#     before, and the agent's own evals as a job of their own."""
#     from app.database.accessor.breeze_buddy.evaluation_config import (
#         get_enabled_evaluations,
#     )
#     from app.services.evals.custom.batch import agent_evals
#
#     try:
#         evaluations = await get_enabled_evaluations(template_id)
#         if not evaluations:
#             return
#         job = ConversationEvaluationJob(
#             source_id=lead_id,
#             channel=ConversationChannel.VOICE,
#             template_id=template_id,  # type: ignore[arg-type]
#         )
#         queue_depth = await _push(job)
#         logger.bind(
#             component=LOG_COMPONENT,
#             source_id=lead_id,
#             template_id=template_id,
#             channel=job.channel.value,
#             queue_depth=queue_depth,
#         ).info(f"Topic evaluation {lead_id} enqueued")
#         if agent_evals(evaluations):
#             queue_depth = await _push(job.model_copy(update={"kind": "evals"}))
#             logger.bind(
#                 component=EVALS_LOG_COMPONENT,
#                 source_id=lead_id,
#                 template_id=template_id,
#                 queue_depth=queue_depth,
#             ).info(f"Agent evals {lead_id} enqueued")
#     except Exception as exc:
#         logger.error(f"Failed to enqueue call evaluations for {lead_id}: {exc}")
#
#
# async def requeue_evals(job: ConversationEvaluationJob) -> None:
#     """Put a failed evals job back at the end of the queue for another try,
#     behind the jobs already waiting."""
#     await _push(job)
