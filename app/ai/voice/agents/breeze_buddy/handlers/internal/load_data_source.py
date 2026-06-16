"""Load Data Source Handler

LLM-callable global function that loads ONE keyed slice of a data source into
context on demand (e.g. a single protocol tab of a multi-tab sheet), instead of
injecting every source up front. Source-agnostic: it dispatches through the
connector registry, so it never mentions "sheet".

Fast path: the slice is usually already in Redis (prefetched at dispatch), so
the mid-call cost is a cache GET — no external round-trip. On a miss it fetches
via the connector and caches the result. Fail-open at every step: a missing /
slow / broken source returns a sentinel and the call continues.

Authored in the template as a global builtin::

    {"type": "builtin", "handler": "load_data_source", "name": "load_protocol",
     "properties": {"source": {"type": "string"}, "key": {"type": "string"}},
     "required": ["source", "key"]}
"""

import asyncio
from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.core.logger import logger
from app.services.data_sources import (
    DATA_SOURCE_UNAVAILABLE,
    get_connector,
    within_cache_limit,
)
from app.services.redis import get_redis_service

_CACHE_TTL = 60  # seconds — matches the inline prefetch cache window
_FETCH_TIMEOUT = 5.0


def _error(source: Optional[str], key: Optional[str], message: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "source": source,
        "key": key,
        "message": message,
        "content": DATA_SOURCE_UNAVAILABLE,
    }


def _unavailable(source: Optional[str], key: Optional[str]) -> Dict[str, Any]:
    return {
        "status": "unavailable",
        "source": source,
        "key": key,
        "content": DATA_SOURCE_UNAVAILABLE,
    }


async def load_data_source(
    context: TemplateContext,
    args: Dict[str, Any],
) -> Dict[str, Any]:
    """Load one keyed slice of a data source and return it to the LLM."""
    source = args.get("source")
    key = args.get("key")

    template = getattr(context.bot, "template", None)
    data_sources = getattr(template, "data_sources", None) or []

    ref = next(
        (d for d in data_sources if d.name == source and d.is_active),
        None,
    )
    if ref is None:
        logger.warning(f"[load_data_source] unknown/inactive source '{source}'")
        return _error(source, key, f"Unknown data source '{source}'")

    connector = get_connector(ref.type)
    if connector is None:
        logger.error(f"[load_data_source] no connector for type '{ref.type}'")
        return _error(source, key, f"No connector registered for '{ref.type}'")

    config = ref.connector_config()
    cache_key = connector.cache_key(config, key)

    # Fast path — prefetched slice already in Redis.
    try:
        redis = await get_redis_service()
        cached = await redis.get(cache_key)
        if cached:
            logger.info(f"[load_data_source] cache hit '{source}' key={key}")
            return {
                "status": "success",
                "source": source,
                "key": key,
                "content": cached,
            }
    except Exception as exc:  # never let cache issues break the call
        logger.warning(f"[load_data_source] cache read failed: {exc}")

    # Miss — fetch the single slice via the connector.
    try:
        content = await asyncio.wait_for(
            connector.fetch(config, key), timeout=_FETCH_TIMEOUT
        )
    except asyncio.TimeoutError:
        logger.warning(f"[load_data_source] fetch timed out '{source}' key={key}")
        return _unavailable(source, key)
    except Exception as exc:
        logger.error(f"[load_data_source] fetch failed '{source}' key={key}: {exc}")
        return _unavailable(source, key)

    if not content or content == DATA_SOURCE_UNAVAILABLE:
        return _unavailable(source, key)

    # Warm the cache for subsequent loads of the same slice (size-capped so a
    # huge slice can't flood Redis and evict live-call state).
    if within_cache_limit(content):
        try:
            redis = await get_redis_service()
            await redis.setex(cache_key, content, ttl_seconds=_CACHE_TTL)
        except Exception as exc:
            logger.warning(f"[load_data_source] cache write failed: {exc}")

    logger.info(
        f"[load_data_source] loaded '{source}' key={key} ({len(content)} chars)"
    )
    return {"status": "success", "source": source, "key": key, "content": content}
