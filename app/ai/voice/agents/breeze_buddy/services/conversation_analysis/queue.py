"""Redis queue for post-conversation evaluations."""

from typing import Any, cast

from app.core.logger import logger
from app.schemas.breeze_buddy.conversation_analysis import (
    ConversationChannel,
    ConversationEvaluationJob,
)
from app.services.redis import get_redis_service

CONVERSATION_EVALUATION_QUEUE = "conversation-evaluation:pending"


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
        await client.rpush(CONVERSATION_EVALUATION_QUEUE, job.model_dump_json())
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
