"""
Database accessor functions for TTS-cache daily metrics.

This module provides business logic for tts_cache_daily operations,
using queries from queries.breeze_buddy.tts_cache and the decoder from
decoder.breeze_buddy.tts_cache.
"""

from datetime import date
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.tts_cache import decode_tts_cache_daily_row
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.tts_cache import (
    get_tts_cache_daily_query,
    upsert_tts_cache_daily_query,
)


async def upsert_tts_cache_daily(
    date_ist: date,
    provider: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: Optional[str],
    hit_requests: int,
    miss_requests: int,
    hit_words: int,
    miss_words: int,
) -> None:
    """Upsert one day's TTS-cache attribution row.

    Args:
        date_ist: The IST calendar day this row aggregates.
        provider: The NESTED provider behind DragonTTS (e.g. cartesia,
            elevenlabs, sarvam, gemini) -- not "dragontts" itself.
        reseller_id: Reseller the activity is attributed to.
        merchant_id: Merchant the activity is attributed to, if known.
        template_id: Template the activity is attributed to, if known.
        hit_requests, miss_requests, hit_words, miss_words: ABSOLUTE
            running totals for the day, as held in the Redis source
            counters. This upsert overwrites, it never increments.

    Raises on failure so the ``bb_ttscache_rollup`` background task can log
    and handle a failed rollup tick rather than silently losing it.
    """
    query, values = upsert_tts_cache_daily_query(
        date_ist=date_ist,
        provider=provider,
        reseller_id=reseller_id,
        merchant_id=merchant_id,
        template_id=template_id,
        hit_requests=hit_requests,
        miss_requests=miss_requests,
        hit_words=hit_words,
        miss_words=miss_words,
    )

    try:
        await run_parameterized_query(query, values)
    except Exception as e:
        logger.error(
            f"Error upserting tts_cache_daily for date_ist={date_ist} "
            f"provider={provider} reseller_id={reseller_id}: {e}"
        )
        raise


def round_ratio(numerator: int, denominator: int) -> Optional[float]:
    """Round numerator/denominator to 4dp, or None when denominator is 0.

    Canonical implementation — the analytics handler imports this for its
    summary totals so both layers round identically.
    """
    if not denominator:
        return None
    return round(numerator / denominator, 4)


async def get_tts_cache_daily(filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Fetch tts_cache_daily rows (raw or grouped, per filters['group_by']).

    Each returned dict is enriched with two derived rates, rounded to 4dp
    and None when their denominator is 0:
      - request_hit_rate = hit_requests / (hit_requests + miss_requests)
      - word_cache_ratio = hit_words / (hit_words + miss_words)

    Args:
        filters: See get_tts_cache_daily_query for supported keys
            (date_from, date_to, reseller_ids, merchant_ids, template_id,
            provider, group_by).

    Returns:
        List of decoded rows, each augmented with request_hit_rate and
        word_cache_ratio.
    """
    query, values = get_tts_cache_daily_query(filters)

    try:
        rows = await run_parameterized_query(query, values)
    except Exception as e:
        logger.error(f"Error fetching tts_cache_daily with filters {filters}: {e}")
        raise

    results: List[Dict[str, Any]] = []
    for row in rows or []:
        decoded = decode_tts_cache_daily_row(row)
        hit_requests = decoded.get("hit_requests") or 0
        miss_requests = decoded.get("miss_requests") or 0
        hit_words = decoded.get("hit_words") or 0
        miss_words = decoded.get("miss_words") or 0

        decoded["request_hit_rate"] = round_ratio(
            hit_requests, hit_requests + miss_requests
        )
        decoded["word_cache_ratio"] = round_ratio(hit_words, hit_words + miss_words)
        results.append(decoded)

    return results
