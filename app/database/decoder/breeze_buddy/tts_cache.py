"""Decoder for tts_cache_daily rows (migration 036).

Unlike most breeze_buddy decoders, this returns a plain dict rather than a
Pydantic model: the SELECT shape varies with the caller's `group_by` filter
(raw per-row columns vs. an aggregate with only date_ist + optionally one
dimension column + the four summed counters), so there is no single fixed
response schema to decode into.
"""

from typing import Any, Dict

import asyncpg


def decode_tts_cache_daily_row(row: asyncpg.Record) -> Dict[str, Any]:
    """Convert a tts_cache_daily row (raw or aggregate) into a plain dict.

    Date/datetime columns are converted to ISO-format strings, matching how
    other decoders in this package hand dates to callers that eventually
    serialize to JSON. Any other columns present (id, provider, reseller_id,
    merchant_id, template_id, the four counters) pass through as-is.
    """
    decoded: Dict[str, Any] = dict(row)

    for key in ("date_ist", "created_at", "updated_at"):
        value = decoded.get(key)
        if value is not None and hasattr(value, "isoformat"):
            decoded[key] = value.isoformat()

    return decoded
