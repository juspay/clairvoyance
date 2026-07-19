"""
Database query functions for TTS-cache daily metrics (migration 036).

This module contains ONLY query generation functions.
Business logic should be in accessor/breeze_buddy/tts_cache.py
"""

from datetime import date
from typing import Any, Dict, List, Optional, Tuple

TTS_CACHE_DAILY_TABLE = "tts_cache_daily"

# Maps a caller-facing `group_by` value to the dimension column to GROUP BY
# alongside date_ist. None means "date only" -- no extra dimension column.
_GROUP_BY_DIMENSION_COLUMN: Dict[str, Optional[str]] = {
    "provider": "provider",
    "merchant": "merchant_id",
    "template": "template_id",
    "date": None,
}


def upsert_tts_cache_daily_query(
    date_ist: date,
    provider: str,
    reseller_id: str,
    merchant_id: Optional[str],
    template_id: Optional[str],
    hit_requests: int,
    miss_requests: int,
    hit_words: int,
    miss_words: int,
) -> Tuple[str, List[Any]]:
    """Generate an upsert query for one day's TTS-cache attribution row.

    The incoming counters are ABSOLUTE day totals (the Redis source holds
    full-day running totals, not deltas) so the ON CONFLICT arm overwrites
    rather than increments. The conflict target mirrors the
    `uq_tts_cache_daily` expression unique index exactly -- COALESCE on the
    nullable merchant_id/template_id dimensions so two rows both missing one
    of those still collide as the same logical bucket.
    """
    query = f"""
        INSERT INTO {TTS_CACHE_DAILY_TABLE} (
            date_ist, provider, reseller_id, merchant_id, template_id,
            hit_requests, miss_requests, hit_words, miss_words, updated_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, NOW())
        ON CONFLICT (date_ist, provider, reseller_id, COALESCE(merchant_id, ''), COALESCE(template_id, ''))
        DO UPDATE SET
            hit_requests = EXCLUDED.hit_requests,
            miss_requests = EXCLUDED.miss_requests,
            hit_words = EXCLUDED.hit_words,
            miss_words = EXCLUDED.miss_words,
            updated_at = NOW()
        RETURNING id
    """
    values = [
        date_ist,
        provider,
        reseller_id,
        merchant_id,
        template_id,
        hit_requests,
        miss_requests,
        hit_words,
        miss_words,
    ]
    return query, values


def get_tts_cache_daily_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Generate a SELECT query for tts_cache_daily rows.

    Supported filters:
      - date_from / date_to: inclusive date_ist range
      - reseller_ids: List[str], matched via = ANY(...)
      - merchant_ids: List[str], matched via = ANY(...)
      - template_id: exact match
      - provider: scalar (exact match) or List[str] (= ANY(...))
      - group_by: optional, one of "provider" | "merchant" | "template" |
        "date". When present, returns an aggregate query that SUMs the four
        counters and GROUPs BY date_ist plus the chosen dimension column
        (no extra dimension column for "date").

    Returns:
        Tuple of (query, values). Ungrouped rows are ordered by
        date_ist DESC, provider; grouped rows by date_ist DESC (then the
        dimension column, if any).
    """
    conditions: List[str] = []
    values: List[Any] = []

    if filters.get("date_from"):
        values.append(filters["date_from"])
        conditions.append(f"date_ist >= ${len(values)}")

    if filters.get("date_to"):
        values.append(filters["date_to"])
        conditions.append(f"date_ist <= ${len(values)}")

    if filters.get("reseller_ids"):
        values.append(filters["reseller_ids"])
        conditions.append(f"reseller_id = ANY(${len(values)})")

    if filters.get("merchant_ids"):
        values.append(filters["merchant_ids"])
        conditions.append(f"merchant_id = ANY(${len(values)})")

    if filters.get("template_id"):
        values.append(filters["template_id"])
        conditions.append(f"template_id = ${len(values)}")

    if filters.get("provider"):
        provider = filters["provider"]
        values.append(provider)
        if isinstance(provider, list):
            conditions.append(f"provider = ANY(${len(values)})")
        else:
            conditions.append(f"provider = ${len(values)}")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    group_by = filters.get("group_by")
    if group_by not in _GROUP_BY_DIMENSION_COLUMN:
        group_by = None

    if group_by:
        dimension_column = _GROUP_BY_DIMENSION_COLUMN[group_by]
        if dimension_column is None:
            select_columns = "date_ist"
            group_columns = "date_ist"
            order_clause = "ORDER BY date_ist DESC"
        else:
            select_columns = f"date_ist, {dimension_column}"
            group_columns = f"date_ist, {dimension_column}"
            order_clause = f"ORDER BY date_ist DESC, {dimension_column}"

        query = f"""
            SELECT
                {select_columns},
                SUM(hit_requests) as hit_requests,
                SUM(miss_requests) as miss_requests,
                SUM(hit_words) as hit_words,
                SUM(miss_words) as miss_words
            FROM {TTS_CACHE_DAILY_TABLE}
            {where_clause}
            GROUP BY {group_columns}
            {order_clause}
        """
    else:
        query = f"""
            SELECT
                id, date_ist, provider, reseller_id, merchant_id, template_id,
                hit_requests, miss_requests, hit_words, miss_words,
                created_at, updated_at
            FROM {TTS_CACHE_DAILY_TABLE}
            {where_clause}
            ORDER BY date_ist DESC, provider
        """

    return query, values
