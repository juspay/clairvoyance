"""
Data Source Prefetch Manager

Pre-warms Redis with Google Sheets content for all DataSourceRefs attached to a
template at dispatch time. Runs concurrently with greeting TTS synthesis.

Cache key is scoped to a content hash so concurrent calls sharing the same sheet
use a single cached copy.

Cache key : ``datasource:{sheet_id}:{sheet_name}:{cols_hash}``
TTL        : 60 s
"""

import asyncio
import hashlib
import json
from typing import Optional

from app.ai.voice.agents.breeze_buddy.template.types import DataSourceRef, TemplateModel
from app.core.logger import logger
from app.services.data_sources import DATA_SOURCE_UNAVAILABLE
from app.services.google.sheets import extract_spreadsheet_id, fetch_formatted
from app.services.redis import get_redis_service

_CACHE_TTL = 60  # seconds — shared across leads; short to keep data fresh
_FETCH_TIMEOUT = 5.0


def _cache_key(ref: DataSourceRef) -> str:
    """Derive a stable cache key from the sheet config."""
    spreadsheet_id = extract_spreadsheet_id(ref.spreadsheet_url)
    if not spreadsheet_id:
        spreadsheet_id = "invalid_url"
    sheet = ref.sheet_name or "_first_"
    cols = json.dumps(sorted(ref.columns or []))
    cols_digest = hashlib.md5(cols.encode()).hexdigest()[:8]
    return f"datasource:{spreadsheet_id}:{sheet}:{cols_digest}:{ref.format.value}"


async def _prefetch_one(ref: DataSourceRef) -> None:
    if not ref.is_active:
        return

    spreadsheet_id = extract_spreadsheet_id(ref.spreadsheet_url)
    if not spreadsheet_id:
        logger.warning("Prefetch: invalid spreadsheet_url for '%s'", ref.name)
        return

    try:
        content = await asyncio.wait_for(
            fetch_formatted(
                spreadsheet_id=spreadsheet_id,
                sheet_name=ref.sheet_name,
                columns=ref.columns,
                format=ref.format.value,
            ),
            timeout=_FETCH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Prefetch timeout for data source '%s'", ref.name)
        return
    except Exception as exc:
        logger.error(
            "Prefetch error for data source '%s': %s", ref.name, exc, exc_info=True
        )
        return

    if not content or content == DATA_SOURCE_UNAVAILABLE:
        return

    cache_key = _cache_key(ref)
    try:
        redis = await get_redis_service()
        await redis.setex(cache_key, content, ttl_seconds=_CACHE_TTL)
        logger.info(
            "Prefetched data source '%s' (%d chars, TTL=%ds)",
            ref.name,
            len(content),
            _CACHE_TTL,
        )
    except Exception as exc:
        logger.error("Failed to cache data source '%s': %s", ref.name, exc)


async def prefetch_data_sources(
    template: Optional[TemplateModel],
) -> None:
    if not template or not template.data_sources:
        return
    await asyncio.gather(
        *[_prefetch_one(ref) for ref in template.data_sources],
        return_exceptions=True,
    )
