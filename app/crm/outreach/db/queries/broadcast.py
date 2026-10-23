"""SQL builders for crm_broadcast (T17, canon/06-outreach.md) — one table,
one file. $1 placeholders only; merchant_id is $1 on every read and write.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.crm.outreach.db.queries.tables import (
    BROADCAST_RECIPIENT_TABLE,
    BROADCAST_TABLE,
    WORKFLOW_TABLE,
)

_BROADCAST_COLUMNS = """
    id, merchant_id, name, campaign_key, segment_id, workflow_id, status,
    scheduled_for, resolved_at, dispatched_at, closed_at, created_by, created_at,
    attempts, last_error
"""
_BROADCAST_COLUMNS_OF_B = ", ".join(
    f"b.{c.strip()}" for c in _BROADCAST_COLUMNS.split(",")
)


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
    queued). The claim spends an attempt (091, the T20 precedent): a freeze
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


def admission_candidates_query(limit: int) -> Tuple[str, List[Any]]:
    """Phase 3, the broadcaster's queue across merchants (the claim_due_runs
    precedent): broadcasts with admission work — dispatching, their workflow
    not paused (a paused plan holds its broadcast's queue, as the walker
    holds its runs), and either a queued row to admit or nothing left
    waiting at all (the last step: sent). One whose only waiting rows are
    claimed is another pod's page, or a dead pod's the reaper will release.
    Oldest broadcast first. No lock: the page claim on the rows is the lock
    (SKIP LOCKED), so two pods share one broadcast safely. The table stays
    small — broadcasts are a merchant's deliberate launches."""
    query = f"""
        SELECT {_BROADCAST_COLUMNS_OF_B}
        FROM {BROADCAST_TABLE} b
        JOIN {WORKFLOW_TABLE} w
          ON w.merchant_id = b.merchant_id AND w.id = b.workflow_id
        WHERE b.status = 'dispatching' AND w.status <> 'paused'
          AND (
            EXISTS (
                SELECT 1 FROM {BROADCAST_RECIPIENT_TABLE} r
                WHERE r.broadcast_id = b.id AND r.status = 'queued'
            )
            OR NOT EXISTS (
                SELECT 1 FROM {BROADCAST_RECIPIENT_TABLE} r
                WHERE r.broadcast_id = b.id AND r.status IN ('queued', 'claimed')
            )
          )
        ORDER BY b.scheduled_for, b.id
        LIMIT $1
    """
    return query, [limit]


def share_lock_broadcast_query(
    merchant_id: str, broadcast_id: str
) -> Tuple[str, List[Any]]:
    """Inside an admission atom: the broadcast row FOR SHARE. Admitting pods
    share it; a cancel (FOR UPDATE) waits for the person being admitted and
    is then seen by the next one — no run starts after the cancel."""
    query = f"""
        SELECT status
        FROM {BROADCAST_TABLE}
        WHERE merchant_id = $1 AND id = $2
        FOR SHARE
    """
    return query, [merchant_id, broadcast_id]


def mark_fired_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    """The first admission: dispatched_at — "fired" (canon T17: "frozen
    Friday 09:58, fired 10:00"). Once: a later page never moves it."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET dispatched_at = now()
        WHERE merchant_id = $1 AND id = $2 AND dispatched_at IS NULL
    """
    return query, [merchant_id, broadcast_id]


def finish_dispatch_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    """Admission is over: dispatching -> sent (canon T17 col 11: "sent =
    dispatch over") — only once no row waits, queued or claimed, so a page
    still in another pod's hands finishes first."""
    query = f"""
        UPDATE {BROADCAST_TABLE} b
        SET status = 'sent'
        WHERE b.merchant_id = $1 AND b.id = $2 AND b.status = 'dispatching'
          AND NOT EXISTS (
            SELECT 1 FROM {BROADCAST_RECIPIENT_TABLE} r
            WHERE r.broadcast_id = b.id AND r.status IN ('queued', 'claimed')
          )
        RETURNING {_BROADCAST_COLUMNS_OF_B}
    """
    return query, [merchant_id, broadcast_id]


def move_broadcast_query(
    merchant_id: str, broadcast_id: str, from_status: str, to_status: str
) -> Tuple[str, List[Any]]:
    """Phase 4's pause and resume: one status to the next, only from the one
    named. The UPDATE's row lock waits for any admission atom holding the
    broadcast FOR SHARE, so the person being admitted finishes and the next
    one sees the new status. No row returned = not this merchant's, or not
    in from_status — the caller reads which."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = $4
        WHERE merchant_id = $1 AND id = $2 AND status = $3
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id, from_status, to_status]


def close_candidates_query(limit: int) -> Tuple[str, List[Any]]:
    """The close job's read, across merchants (the claim_due_freezes
    precedent): sent broadcasts that may have reached the end of their
    reporting window. Every window is at least a day past the fire
    (broadcasts.CLOSE_GRACE), so a broadcast fired less than a day ago is
    never read; one that admitted nobody (dispatched_at NULL) is read at
    once. The job decides each one's moment from its workflow. Sent
    broadcasts are few — a merchant's deliberate launches, closed within
    days — so the read stays small."""
    query = f"""
        SELECT {_BROADCAST_COLUMNS}
        FROM {BROADCAST_TABLE}
        WHERE status = 'sent'
          AND (dispatched_at IS NULL
               OR dispatched_at <= now() - make_interval(days => 1))
        ORDER BY dispatched_at NULLS FIRST, id
        LIMIT $1
    """
    return query, [limit]


def close_broadcast_query(merchant_id: str, broadcast_id: str) -> Tuple[str, List[Any]]:
    """The reporting window ended: sent -> closed, closed_at stamped (canon
    T17 col 11: "closed = the reporting window ended and the funnel went
    final"). Only from sent, so a second job never closes it twice."""
    query = f"""
        UPDATE {BROADCAST_TABLE}
        SET status = 'closed', closed_at = now()
        WHERE merchant_id = $1 AND id = $2 AND status = 'sent'
        RETURNING {_BROADCAST_COLUMNS}
    """
    return query, [merchant_id, broadcast_id]
