"""SQL builders for crm_broadcast_recipient (T18, canon/06-outreach.md) — one
table, one file. A pure child row with no merchant_id: every write that
decides a row's fate runs inside an atom that has already locked the parent
broadcast under merchant_id = $1 (lock_broadcast_query), and every read
enters through the parent — joined on ``crm_broadcast.merchant_id = $1``
(the T21 precedent). Admission's claim, its hold and the reaper are the
broadcaster's queue across merchants (the claim_due_runs precedent): they
only move a row between queued and claimed, keyed by a broadcast the
candidates read already scoped.
"""

from typing import Any, List, Optional, Tuple

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


def claim_page_query(
    broadcast_id: str, claimed_by: str, limit: int
) -> Tuple[str, List[Any]]:
    """Admission's claim (canon T18 cols 10-11, canon/06-outreach.md: "workers
    claim with FOR UPDATE SKIP LOCKED"): the next page of a broadcast's
    queued rows becomes claimed — by whom, since when. Two pods never take
    one person; the partial waiting index (091) walks only rows still
    waiting, in key order."""
    query = f"""
        UPDATE {BROADCAST_RECIPIENT_TABLE} r
        SET status = 'claimed', claimed_by = $2, claimed_at = now()
        FROM (
            SELECT customer_id
            FROM {BROADCAST_RECIPIENT_TABLE}
            WHERE broadcast_id = $1 AND status = 'queued'
            ORDER BY customer_id
            LIMIT $3
            FOR UPDATE SKIP LOCKED
        ) AS page
        WHERE r.broadcast_id = $1 AND r.customer_id = page.customer_id
        RETURNING r.customer_id
    """
    return query, [broadcast_id, claimed_by, limit]


def settle_recipient_query(
    broadcast_id: str, customer_id: str, status: str, skip_reason: Optional[str]
) -> Tuple[str, List[Any]]:
    """Inside admission's per-person atom: a claimed row's end — enrolled,
    skipped with its reason, or cancelled. Only from claimed: a row the
    reaper already released is not this worker's to settle."""
    query = f"""
        UPDATE {BROADCAST_RECIPIENT_TABLE}
        SET status = $3, skip_reason = $4
        WHERE broadcast_id = $1 AND customer_id = $2 AND status = 'claimed'
        RETURNING customer_id
    """
    return query, [broadcast_id, customer_id, status, skip_reason]


def release_claims_query(
    broadcast_id: str, customer_ids: List[str]
) -> Tuple[str, List[Any]]:
    """A held page goes back to the queue (its workflow was paused after the
    claim): claimed -> queued, nobody holding it."""
    query = f"""
        UPDATE {BROADCAST_RECIPIENT_TABLE}
        SET status = 'queued', claimed_by = NULL, claimed_at = NULL
        WHERE broadcast_id = $1 AND customer_id = ANY($2::uuid[])
          AND status = 'claimed'
    """
    return query, [broadcast_id, customer_ids]


def reap_stale_claims_query(lease_minutes: int) -> Tuple[str, List[Any]]:
    """The reaper (canon T18 col 11: "releases anything claimed longer than
    N minutes"): a pod that died mid-page leaves its rows claimed; after the
    lease they are queued again and the next page takes them. Safe to
    repeat a person: the run and its "enrolled" share one atom, so a row
    still claimed never started a run."""
    query = f"""
        UPDATE {BROADCAST_RECIPIENT_TABLE}
        SET status = 'queued', claimed_by = NULL, claimed_at = NULL
        WHERE status = 'claimed'
          AND claimed_at < now() - make_interval(mins => $1)
        RETURNING broadcast_id
    """
    return query, [lease_minutes]
