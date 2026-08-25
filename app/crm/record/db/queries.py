"""SQL builders for crm_event_raw (T13) and crm_journey_event (V01, A12).
$1 placeholders only."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

EVENT_RAW_TABLE = "crm_event_raw"
JOURNEY_EVENT_VIEW = "crm_journey_event"


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


def get_customer_journey_query(
    merchant_id: str,
    customer_id: str,
    limit: int,
    before_started_at: Optional[datetime] = None,
    before_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Keyset cursor (started_at, id) — canon's prescribed pagination for
    V01 (crm.journey_event). An OFFSET page shifts under an append-heavy
    timeline; a row-value comparison on the cursor doesn't."""
    values: List[Any] = [merchant_id, customer_id]
    cursor = ""
    if before_started_at is not None and before_id:
        values.extend([before_started_at, before_id])
        cursor = "AND (started_at, id) < ($3, $4)"
    values.append(limit)
    query = f"""
        SELECT id, merchant_id, customer_id, channel, direction, handled_by,
               started_at, ended_at, outcome, recording_ref, transcript_ref,
               source_kind
        FROM {JOURNEY_EVENT_VIEW}
        WHERE merchant_id = $1 AND customer_id = $2
        {cursor}
        ORDER BY started_at DESC, id DESC
        LIMIT ${len(values)}
    """
    return query, values
