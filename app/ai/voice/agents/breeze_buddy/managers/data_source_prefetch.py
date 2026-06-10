"""
Data Source Prefetch Manager

Pre-warms Redis with Google Sheets content for all DataSourceRefs attached to a
template at dispatch time.  This runs concurrently with greeting TTS synthesis so
that sheet content is already cached before the call connects.

Cache key : ``datasource:{lead_id}:{ref.name}``
TTL        : 300 s  (covers typical call duration + re-try window)
"""

import asyncio
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef, TemplateModel
from app.core.logger import logger
from app.database.accessor.breeze_buddy.data_source import get_data_source_by_id
from app.services.google.sheets import fetch_formatted
from app.services.redis import get_redis_service

_CACHE_TTL = 300  # seconds
_FETCH_TIMEOUT = 5.0  # generous timeout for background prefetch


async def _prefetch_one(lead_id: str, ref: DataSourceRef) -> None:
    """Fetch and cache content for a single DataSourceRef."""
    cache_key = f"datasource:{lead_id}:{ref.name}"
    try:
        ds = await get_data_source_by_id(ref.data_source_id)
        if not ds:
            logger.warning(
                "Prefetch: data source %s not found in DB (ref name=%s)",
                ref.data_source_id,
                ref.name,
            )
            return

        content = await asyncio.wait_for(
            fetch_formatted(
                spreadsheet_id=ds.spreadsheet_id,
                sheet_name=ds.sheet_name,
                columns=ds.columns,
                format=ds.format,
            ),
            timeout=_FETCH_TIMEOUT,
        )

        redis = await get_redis_service()
        await redis.setex(cache_key, content, ttl_seconds=_CACHE_TTL)
        logger.info(
            "Prefetched data source '%s' for lead=%s (%d chars, TTL=%ds)",
            ref.name,
            lead_id,
            len(content),
            _CACHE_TTL,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Prefetch timeout for data source '%s', lead=%s", ref.name, lead_id
        )
    except Exception as exc:
        logger.error(
            "Prefetch error for data source '%s', lead=%s: %s",
            ref.name,
            lead_id,
            exc,
            exc_info=True,
        )


async def prefetch_data_sources(
    lead_id: str,
    template: Optional[TemplateModel],
) -> None:
    """
    Pre-warm Redis with sheet content for every DataSourceRef on *template*.

    Safe to call even when template is None or has no data_sources — it
    silently returns without doing any work.
    """
    if not template or not template.data_sources:
        return

    await asyncio.gather(
        *[_prefetch_one(lead_id, ref) for ref in template.data_sources],
        return_exceptions=True,
    )
