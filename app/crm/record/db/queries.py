"""SQL builders for crm_event_raw (T13). $1 placeholders only."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

EVENT_RAW_TABLE = "crm_event_raw"


def insert_event_query(
    merchant_id: str,
    source: str,
    topic: str,
    external_id: str,
    payload: str,
    schema_version: str,
    occurred_at: Optional[datetime],
    customer_id: Optional[str],
) -> Tuple[str, List[Any]]:
    """Store the letter raw. Dedupe rides the UNIQUE (merchant_id, source,
    external_id): a conflict is a silent drop (no row returned), still OK.
    occurred_at is the source's claim, clamped never later than
    received_at (LEAST against now())."""
    query = f"""
        INSERT INTO {EVENT_RAW_TABLE}
            (merchant_id, source, topic, external_id, payload,
             schema_version, occurred_at, customer_id)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6, LEAST($7, now()), $8)
        ON CONFLICT (merchant_id, source, external_id) DO NOTHING
        RETURNING id
    """
    return query, [
        merchant_id,
        source,
        topic,
        external_id,
        payload,
        schema_version,
        occurred_at,
        customer_id,
    ]
