"""SQL builders for crm_broadcast_recipient (T18, canon/06-outreach.md) — one
table, one file. A pure child row with no merchant_id: every statement here
runs inside an atom that has already locked the parent broadcast under
merchant_id = $1 (lock_broadcast_query), the T21 precedent.
"""

from typing import Any, List, Tuple

from app.crm.outreach.db.queries.tables import BROADCAST_RECIPIENT_TABLE


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
    """A cancelled broadcast admits nobody else: every row still queued is
    cancelled. Rows already enrolled stay — their runs resolve by their own
    rules (canon: cancelling a blast stops future admissions)."""
    query = f"""
        UPDATE {BROADCAST_RECIPIENT_TABLE}
        SET status = 'cancelled'
        WHERE broadcast_id = $1 AND status = 'queued'
        RETURNING customer_id
    """
    return query, [broadcast_id]
