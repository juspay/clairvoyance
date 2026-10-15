"""SQL builders for crm_broadcast (T17, canon/06-outreach.md) — one table,
one file. $1 placeholders only; merchant_id is $1 on every read and write.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import BROADCAST_TABLE

_BROADCAST_COLUMNS = """
    id, merchant_id, name, campaign_key, segment_id, workflow_id, status,
    scheduled_for, resolved_at, dispatched_at, closed_at, created_by, created_at,
    attempts, last_error
"""


def insert_broadcast_query(
    merchant_id: str,
    name: str,
    segment_id: str,
    workflow_id: str,
    campaign_key: Optional[str],
    created_by: Optional[str],
) -> Tuple[str, List[Any]]:
    """A broadcast is born a draft: nothing scheduled, nothing frozen — the
    clocks after created_at stay NULL until the launch (phase 2) sets them."""
    query = f"""
        INSERT INTO {BROADCAST_TABLE}
            (merchant_id, name, segment_id, workflow_id, campaign_key, created_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, name, segment_id, workflow_id, campaign_key, created_by]


def get_broadcast_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_BROADCAST_COLUMNS}
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND id = $2
    """
    return query, [merchant_id, broadcast_id]


def list_broadcasts_query(
    merchant_id: str, campaign_key: Optional[str], limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """Newest first — crm_broadcast_merchant_created_ix, or with a badge the
    campaign-level read on crm_broadcast_campaign_ix."""
    values: List[Any] = [merchant_id]
    badge = ""
    if campaign_key is not None:
        values.append(campaign_key)
        badge = "AND campaign_key = $2"
    values += [limit, offset]
    query = f"""
        SELECT {_BROADCAST_COLUMNS}
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 {badge}
        ORDER BY created_at DESC, id DESC
        LIMIT ${len(values) - 1} OFFSET ${len(values)}
    """
    return query, values


def count_broadcasts_query(
    merchant_id: str, campaign_key: Optional[str]
) -> Tuple[str, List[Any]]:
    """The list's total, under the same badge filter (the RunPage ruling)."""
    if campaign_key is None:
        return (
            f"SELECT count(*)::int AS total FROM {BROADCAST_TABLE} WHERE merchant_id = $1",
            [merchant_id],
        )
    query = f"""
        SELECT count(*)::int AS total
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND campaign_key = $2
    """
    return query, [merchant_id, campaign_key]


def schedule_broadcast_query(
    merchant_id: str, broadcast_id: str, scheduled_for: Optional[datetime]
) -> Tuple[str, List[Any]]:
    """Aim a draft (or re-aim a scheduled broadcast): scheduled_for, or now
    when none is given. No row returned = not this merchant's, or already
    launched or finished — the caller reads which."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'scheduled', scheduled_for = COALESCE($3::timestamptz, now())
        WHERE merchant_id = $1 AND id = $2 AND status IN ('draft', 'scheduled')
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id, scheduled_for]


def lock_broadcast_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    """Inside an atom: the broadcast row FOR UPDATE, so a cancel and a
    freeze page take turns — a page never lands after the cancel that
    cancelled its queue."""
    query = f"""
        SELECT status
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND id = $2
        FOR UPDATE
    """
    return query, [merchant_id, broadcast_id]


def cancel_broadcast_query(
    merchant_id: str, broadcast_id: str
) -> Tuple[str, List[Any]]:
    """Inside the cancel atom, after the lock read the status."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'cancelled'
        WHERE merchant_id = $1 AND id = $2
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id]


def claim_due_freezes_query(limit: int, lease_minutes: int) -> Tuple[str, List[Any]]:
    """The freeze's claim, across merchants (a worker's queue, the
    claim_due_runs precedent): broadcasts whose moment has come, and
    freezes whose worker died mid-way — resolved_at is the lease, renewed by
    every page, so a dead freeze is claimed again once it is lease_minutes
    old (the re-run resumes, and the recipients' PK absorbs who is already
    queued). The claim spends an attempt (092, the T20 precedent): a freeze
    that keeps failing counts against itself. SKIP LOCKED: replicas never
    collide."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'resolving', resolved_at = now(), attempts = attempts + 1
        WHERE id IN (
            SELECT id FROM {BROADCAST_TABLE}
            WHERE (status = 'scheduled' AND scheduled_for <= now())
               OR (status = 'resolving'
                   AND resolved_at <= now() - make_interval(mins => $2))
            ORDER BY scheduled_for, id
            LIMIT $1
            FOR UPDATE SKIP LOCKED
        )
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [limit, lease_minutes]


def renew_freeze_lease_query(
    merchant_id: str, broadcast_id: str
) -> Tuple[str, List[Any]]:
    """Inside a freeze page's atom: the lease moves on with every page, so a
    freeze longer than the lease is never taken over while it still runs.
    The last page's renewal is when the audience was frozen (canon T17 col
    14). Only from resolving."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET resolved_at = now()
        WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'
    """
    return query, [merchant_id, broadcast_id]


def finish_freeze_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    """The audience is frozen: resolving -> dispatching. dispatched_at stays
    NULL — "fired" is the first admission (canon T17 col 18: frozen 09:58,
    fired 10:00), phase 3's to stamp. attempts and last_error are cleared,
    as a run's are by the move that succeeds (T20's advance): a retry that
    worked leaves no error on the console. Only from resolving — a cancel
    that won the race keeps its word."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'dispatching', attempts = 0, last_error = NULL
        WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id]


def fail_freeze_query(
    merchant_id: str, broadcast_id: str, last_error: str
) -> Tuple[str, List[Any]]:
    """The freeze cannot finish — the segment cannot be answered (canon:
    stuck resolving is a segment problem), or its attempts ran out:
    resolving -> failed, with the reason readable on the broadcast. Only
    from resolving."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'failed', last_error = $3
        WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id, last_error]


def record_freeze_error_query(
    merchant_id: str, broadcast_id: str, last_error: str
) -> Tuple[str, List[Any]]:
    """A freeze attempt broke (a replica timeout, a bug): why, kept on the
    broadcast, which stays resolving for the next claim once the lease runs
    out. Only from resolving."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET last_error = $3
        WHERE merchant_id = $1 AND id = $2 AND status = 'resolving'
    """
    return query, [merchant_id, broadcast_id, last_error]
