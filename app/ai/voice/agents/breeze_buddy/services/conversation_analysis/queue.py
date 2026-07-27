"""Redis queue for post-conversation topic analysis."""

from typing import Any, cast

from app.core.logger import logger
from app.services.redis import get_redis_service

TOPIC_ANALYSIS_QUEUE = "conversation-analysis:pending"


async def enqueue_topic_analysis(analysis_id: str) -> None:
    try:
        redis = await get_redis_service()
        client: Any = cast(Any, await redis.get_client())
        await client.rpush(TOPIC_ANALYSIS_QUEUE, analysis_id)
    except Exception as exc:
        logger.error(f"Failed to enqueue topic analysis {analysis_id}: {exc}")


async def dequeue_topic_analysis() -> str:
    redis = await get_redis_service()
    client: Any = cast(Any, await redis.get_client())
    popped = await client.blpop(
        TOPIC_ANALYSIS_QUEUE,
        timeout=0,
    )
    return str(popped[1])
