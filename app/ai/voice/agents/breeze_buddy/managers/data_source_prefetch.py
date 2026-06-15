"""
Data Source Prefetch Manager

Pre-warms Redis with Google Sheets content for all DataSourceRefs attached to a
template at dispatch time. Runs concurrently with greeting TTS synthesis.

Cache key is scoped to the data_source_id (not lead_id) so that concurrent
calls referencing the same sheet share a single cached copy.

Cache key : ``datasource:content:{data_source_id}``
TTL        : 60 s (short: keeps data fresh, covers burst window)
"""

import asyncio
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef, TemplateModel
from app.core.logger import logger
from app.database.accessor.breeze_buddy.data_source import get_data_source_by_id
from app.services.data_sources import data_source_in_template_scope
from app.services.google.sheets import fetch_formatted
from app.services.redis import get_redis_service

_CACHE_TTL = 60  # seconds — shared across leads; short to keep data fresh
_FETCH_TIMEOUT = 5.0  # generous timeout for background prefetch


async def _prefetch_one(
    lead_id: str, template: TemplateModel, ref: DataSourceRef
) -> None:
    """Fetch and cache content for a single DataSourceRef."""
    cache_key = f"datasource:content:{ref.data_source_id}"
    try:
        ds = await get_data_source_by_id(ref.data_source_id)
        if not ds:
            logger.warning(
                "Prefetch: data source %s not found in DB (ref name=%s)",
                ref.data_source_id,
                ref.name,
            )
            return
        if not data_source_in_template_scope(
            ds, template.reseller_id, template.merchant_id
        ):
            logger.warning(
                "Prefetch: data source %s is inactive or outside template scope "
                "(template=%s, ref name=%s)",
                ref.data_source_id,
                template.id,
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
            "Prefetched data source '%s' (ds=%s, %d chars, TTL=%ds)",
            ref.name,
            ref.data_source_id,
            len(content),
            _CACHE_TTL,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Prefetch timeout for data source '%s' (ds=%s)",
            ref.name,
            ref.data_source_id,
        )
    except Exception as exc:
        logger.error(
            "Prefetch error for data source '%s' (ds=%s): %s",
            ref.name,
            ref.data_source_id,
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
        *[_prefetch_one(lead_id, template, ref) for ref in template.data_sources],
        return_exceptions=True,
    )
