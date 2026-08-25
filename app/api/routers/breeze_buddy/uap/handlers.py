import json
from typing import Any, Dict

from fastapi import Request

from app.core.logger import logger
from app.services.redis import get_redis_service, is_redis_configured

_DEDUPE_TTL_SECONDS = 7 * 24 * 3600


async def _is_duplicate(event_key: str) -> bool:
    if not is_redis_configured():
        return False
    redis = await get_redis_service()
    was_set = await redis.set(
        f"uap:webhook:{event_key}", "1", nx=True, ex=_DEDUPE_TTL_SECONDS
    )
    if was_set:
        return False

    return await redis.get(f"uap:webhook:{event_key}") is not None


async def handle_webhook(request: Request) -> Dict[str, Any]:
    raw = await request.body()
    try:
        event = json.loads(raw)
    except ValueError:
        logger.error(f"UAP webhook non-JSON body ({len(raw)} bytes)")
        return {"status": "ignored"}

    event_id = str(event.get("id") or "")
    event_name = str(event.get("event_name") or "")
    order = (event.get("content") or {}).get("order") or {}
    order_id = str(order.get("order_id") or order.get("id") or "")

    dedupe_key = event_id or f"{order_id}:{event_name}:{order.get('status', '')}"
    if dedupe_key and await _is_duplicate(dedupe_key):
        logger.info(f"UAP webhook duplicate skipped id={event_id} order={order_id}")
        return {"status": "success"}

    logger.info(
        f"UAP webhook event={event_name} order_id={order_id} "
        f"status={order.get('status')} id={event_id}"
    )

    return {"status": "success"}
