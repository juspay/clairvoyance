"""
Data Source Prefetch Manager

Pre-warms Redis with data source content for all DataSourceRefs attached to a
template at dispatch time. Runs concurrently with greeting TTS synthesis.

All fetching and cache-key derivation go through the connector registry (one
source of truth shared with the loader and the load_data_source builtin), so
eager and on-demand paths can never drift.

TTL : 60 s
"""

import asyncio
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import (
    DataSourceMode,
    DataSourceRef,
    TemplateModel,
)
from app.core.logger import logger
from app.services.data_sources import (
    DATA_SOURCE_UNAVAILABLE,
    DataSourceConnector,
    get_connector,
    within_cache_limit,
)
from app.services.redis import get_redis_service

_CACHE_TTL = 60  # seconds — shared across leads; short to keep data fresh
_FETCH_TIMEOUT = 5.0


async def _prefetch_one(ref: DataSourceRef) -> None:
    """Eager source: warm its single configured slice via the connector."""
    if not ref.is_active:
        return
    connector = get_connector(ref.type)
    if connector is None:
        logger.warning(f"Prefetch: no connector for type '{ref.type}'")
        return
    # key=None → the connector uses the configured slice (sheet_name in config).
    await _cache_slice(connector, ref.connector_config(), ref, None)


async def _cache_slice(
    connector: DataSourceConnector,
    config: dict,
    ref: DataSourceRef,
    key: Optional[str],
) -> None:
    """Fetch a single keyed slice via the connector and cache it (fail-open)."""
    try:
        content = await asyncio.wait_for(
            connector.fetch(config, key), timeout=_FETCH_TIMEOUT
        )
    except Exception as exc:
        logger.warning(f"Prefetch slice '{ref.name}' key={key} failed: {exc}")
        return

    if not content or content == DATA_SOURCE_UNAVAILABLE:
        return

    if not within_cache_limit(content):
        logger.warning(
            f"Prefetch slice '{ref.name}' key={key} too large to cache "
            f"({len(content.encode('utf-8'))} bytes); skipping"
        )
        return

    try:
        redis = await get_redis_service()
        await redis.setex(
            connector.cache_key(config, key), content, ttl_seconds=_CACHE_TTL
        )
    except Exception as exc:
        logger.error(f"Failed to cache slice '{ref.name}' key={key}: {exc}")


async def _prefetch_on_demand(ref: DataSourceRef) -> None:
    """Warm every keyed slice (e.g. every sheet tab) of an on-demand source so
    the agent's first mid-call ``load_data_source`` is a cache hit, not a fetch.
    """
    if not ref.is_active:
        return

    connector = get_connector(ref.type)
    if connector is None:
        logger.error(f"Prefetch: no connector for type '{ref.type}'")
        return

    config = ref.connector_config()
    try:
        keys = await asyncio.wait_for(
            connector.list_keys(config), timeout=_FETCH_TIMEOUT
        )
    except Exception as exc:
        logger.warning(f"Prefetch: list_keys failed for '{ref.name}': {exc}")
        return

    if not keys:
        return

    await asyncio.gather(
        *[_cache_slice(connector, config, ref, key) for key in keys],
        return_exceptions=True,
    )
    logger.info(
        f"Prefetched on-demand source '{ref.name}': "
        f"{len(keys)} slice(s), TTL={_CACHE_TTL}s"
    )


async def prefetch_data_sources(
    template: Optional[TemplateModel],
) -> None:
    if not template or not template.data_sources:
        return
    tasks = []
    for ref in template.data_sources:
        if ref.mode == DataSourceMode.ON_DEMAND:
            tasks.append(_prefetch_on_demand(ref))
        else:
            tasks.append(_prefetch_one(ref))
    await asyncio.gather(*tasks, return_exceptions=True)
