"""SQL builders for crm_message_receipt_pending — receipts parked until their
message's row knows the provider's id (migration 082)."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.crm.connectivity.db.queries.message import MESSAGE_TABLE

PENDING_TABLE = "crm_message_receipt_pending"


def park_receipt_query(
    merchant_id: str,
    provider_message_id: str,
    state: str,
    occurred_at: Optional[datetime],
    error_code: Optional[str],
    pricing_category: Optional[str],
) -> Tuple[str, List[Any]]:
    """Park one receipt. A duplicate (same message, same state) parks nothing
    new — the first one is as good as the second."""
    query = f"""
        INSERT INTO {PENDING_TABLE}
               (merchant_id, provider_message_id, state, occurred_at,
                error_code, pricing_category)
        VALUES ($1, $2, $3, $4::timestamptz, $5, $6)
        ON CONFLICT (merchant_id, provider_message_id, state) DO NOTHING
    """
    return query, [
        merchant_id,
        provider_message_id,
        state,
        occurred_at,
        error_code,
        pricing_category,
    ]


def lock_parked_query(
    merchant_id: str, provider_message_id: str
) -> Tuple[str, List[Any]]:
    """The receipts parked for one message, locked for the atom that
    applies them — NOT deleted: a row leaves only once its receipt has been
    applied, so a failure part-way rolls back and loses nothing. SKIP LOCKED:
    a second drainer (the sweep, a concurrent stamp) takes what the first is
    not already applying."""
    query = f"""
        SELECT state, occurred_at, error_code, pricing_category
          FROM {PENDING_TABLE}
         WHERE merchant_id = $1 AND provider_message_id = $2
           FOR UPDATE SKIP LOCKED
    """
    return query, [merchant_id, provider_message_id]


def parked_messages_query(limit: int) -> Tuple[str, List[Any]]:
    """The messages with receipts parked whose row now carries the provider's
    id — the ones a drain can finish, oldest first. A foreign receipt (no row
    will ever match) is never picked, so it cannot crowd a real one out of
    the batch while it waits out its grace (crm_message's provider-id index,
    migration 056, serves the probe)."""
    query = f"""
        SELECT p.merchant_id, p.provider_message_id
          FROM {PENDING_TABLE} p
         WHERE EXISTS (
                   SELECT 1 FROM {MESSAGE_TABLE} m
                    WHERE m.provider_message_id = p.provider_message_id
                      AND m.merchant_id = p.merchant_id
               )
         GROUP BY p.merchant_id, p.provider_message_id
         ORDER BY min(p.parked_at)
         LIMIT $1
    """
    return query, [limit]


def unpark_query(
    merchant_id: str, provider_message_id: str, state: str
) -> Tuple[str, List[Any]]:
    query = f"""
        DELETE FROM {PENDING_TABLE}
         WHERE merchant_id = $1 AND provider_message_id = $2 AND state = $3
    """
    return query, [merchant_id, provider_message_id, state]


def expire_parked_query(grace_seconds: int) -> Tuple[str, List[Any]]:
    """Drop receipts parked past the grace: they name messages this system
    did not send. Cross-tenant by design (the dispatcher's sweep)."""
    query = f"""
        WITH gone AS (
            DELETE FROM {PENDING_TABLE}
             WHERE parked_at < now() - make_interval(secs => $1::int)
            RETURNING 1
        )
        SELECT count(*) FROM gone
    """
    return query, [grace_seconds]
