"""SQL builders for crm_broadcast_recipient (T18, canon/06-outreach.md) — one
table, one file. A pure child row with no merchant_id: every write runs
inside an atom that has already locked the parent broadcast under
merchant_id = $1 (lock_broadcast_query), and every read enters through the
parent — joined on ``crm_broadcast.merchant_id = $1`` (the T21 precedent).
"""

from typing import Any, List, Tuple

from app.crm.outreach.db.queries.tables import (
    BROADCAST_RECIPIENT_TABLE,
    BROADCAST_TABLE,
)


def insert_recipients_query(
    broadcast_id: str, customer_ids: List[str]
) -> Tuple[str, List[Any]]:
    """One freeze page in ONE statement, queued. The composite PK is the
    dedupe: a re-run freeze (or a person on two pages) is a no-op. The
    statement answers with how many rows it really added — one number, not
    the page's ids sent back (a freeze needs the count, never the rows)."""
    query = f"""
        WITH added AS (
            INSERT INTO {BROADCAST_RECIPIENT_TABLE} (broadcast_id, customer_id)
            SELECT $1, customer_id
            FROM unnest($2::uuid[]) AS customer_id
            ON CONFLICT (broadcast_id, customer_id) DO NOTHING
            RETURNING 1
        )
        SELECT count(*) AS added FROM added
    """
    return query, [broadcast_id, customer_ids]


def cancel_queued_recipients_query(broadcast_id: str) -> Tuple[str, List[Any]]:
    """A cancelled (or failed) broadcast admits nobody else: every row still
    queued is cancelled. Rows already enrolled stay — their runs resolve by
    their own rules (canon: cancelling a blast stops future admissions). It
    answers with one count, not every id: a 2-lakh queue never comes back to
    Python as 2 lakh rows (the freeze's insert, the same shape)."""
    query = f"""
        WITH cancelled AS (
            UPDATE {BROADCAST_RECIPIENT_TABLE}
            SET status = 'cancelled'
            WHERE broadcast_id = $1 AND status = 'queued'
            RETURNING 1
        )
        SELECT count(*) AS cancelled FROM cancelled
    """
    return query, [broadcast_id]


def last_frozen_customer_query(
    merchant_id: str, broadcast_id: str
) -> Tuple[str, List[Any]]:
    """Where a freeze resumes: the largest customer id already frozen for
    this broadcast, whatever the row's status (a freeze only resumes while
    the broadcast is resolving, before any row is admitted). Segment pages
    are keyset in ascending customer_id, so the next page starts right after
    it. One backward step on the PK."""
    query = f"""
        SELECT r.customer_id
        FROM {BROADCAST_RECIPIENT_TABLE} r
        JOIN {BROADCAST_TABLE} b ON b.id = r.broadcast_id
        WHERE b.merchant_id = $1 AND r.broadcast_id = $2
        ORDER BY r.customer_id DESC
        LIMIT 1
    """
    return query, [merchant_id, broadcast_id]
