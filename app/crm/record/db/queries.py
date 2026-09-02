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


def claim_pending_events_query(limit: int) -> Tuple[str, List[Any]]:
    """The lock IS the claim: FOR UPDATE SKIP LOCKED means concurrent
    event-worker replicas never fight over the same row, and the lock is
    held by the enclosing transaction (_pass_in_txn) until its commit.
    Ordered by received_at to match crm_event_raw_pending_ix (T13) — the
    CTE keeps that order on the rows handed back, which an UPDATE's
    RETURNING alone does not promise.

    The claim SPENDS an attempt (migration 062; the enrollment claim's
    shape, 058): counted by the claim, not by the failure, so a crash
    mid-row counts against the row and a poison row can no longer hide
    at the head of the queue — the worker quarantines it once attempts
    reach CRM_EVENT_MAX_ATTEMPTS."""
    query = f"""
        WITH claimed AS (
            UPDATE {EVENT_RAW_TABLE}
            SET attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM {EVENT_RAW_TABLE}
                WHERE processed_at IS NULL
                ORDER BY received_at
                LIMIT $1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, merchant_id, source, topic, schema_version,
                      external_id, payload, received_at, occurred_at,
                      customer_id, attempts
        )
        SELECT * FROM claimed ORDER BY received_at
    """
    return query, [limit]


def stamp_event_query(
    event_id: str, customer_id: Optional[str]
) -> Tuple[str, List[Any]]:
    """Touches only customer_id + processed_at — the immutability trigger
    (T13) allows nothing else on this table."""
    query = f"""
        UPDATE {EVENT_RAW_TABLE}
        SET customer_id = $2, processed_at = now()
        WHERE id = $1
    """
    return query, [event_id, customer_id]


def quarantine_event_query(event_id: str, reason: str) -> Tuple[str, List[Any]]:
    """Touches only quarantine_reason + processed_at — same trigger."""
    query = f"""
        UPDATE {EVENT_RAW_TABLE}
        SET quarantine_reason = $2, processed_at = now()
        WHERE id = $1
    """
    return query, [event_id, reason]


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


def customer_has_event_query(
    merchant_id: str, customer_id: str, topics: List[str], since: datetime
) -> Tuple[str, List[Any]]:
    """The walker's goal re-check at fire time: did she do the thing after
    the run began? One EXISTS on the stamped customer index. A producer
    that omits occurred_at must not defeat the check (NULL > x is NULL,
    never true) — the envelope's received_at stands in."""
    query = f"""
        SELECT EXISTS (
            SELECT 1 FROM {EVENT_RAW_TABLE}
            WHERE merchant_id = $1
              AND customer_id = $2
              AND topic = ANY($3)
              AND COALESCE(occurred_at, received_at) > $4
        ) AS found
    """
    return query, [merchant_id, customer_id, topics, since]
