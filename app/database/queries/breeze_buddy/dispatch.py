"""
Database queries for the event-driven dispatcher.

Distinct module so the dispatcher's needs don't bloat the main
``lead_call_tracker`` query file. All queries follow the project pattern:
``Tuple[str, List[Any]]`` of SQL text and parameterised values.
"""

from datetime import datetime
from typing import Any, List, Tuple

from app.database.queries.breeze_buddy.lead_call_tracker import (
    LEAD_CALL_TRACKER_TABLE,
)


def get_unscheduled_backlog_leads_query(
    lookahead_seconds: int, limit: int
) -> Tuple[str, List[Any]]:
    """
    For ``reconcile_backlog_to_zset``: find BACKLOG rows that should be on
    the schedule. Bounded by a small lookahead window so the scan is cheap
    even on a large table — far-future leads are handled by subsequent
    reconciler ticks as their firing time approaches.
    """
    text = f"""
        SELECT id, reseller_id, EXTRACT(EPOCH FROM next_attempt_at) * 1000 AS score_ms
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = 'BACKLOG'
          AND "is_locked" = FALSE
          AND "execution_mode" IN ('TELEPHONY', 'TELEPHONY_TEST')
          AND "next_attempt_at" <= NOW() + ($1 || ' seconds')::interval
        ORDER BY "next_attempt_at" ASC
        LIMIT $2;
    """
    return text, [str(lookahead_seconds), limit]


def count_processing_by_outbound_number_query() -> Tuple[str, List[Any]]:
    """
    For ``reconcile_channel_tokens``: how many calls are PROCESSING right
    now for each outbound number? The reconciler compares this against
    LLEN of the channel LIST and tops up or trims to maintain
    ``M - in_flight == LLEN``.
    """
    text = f"""
        SELECT "outbound_number_id", COUNT(*) AS in_flight
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = 'PROCESSING'
          AND "outbound_number_id" IS NOT NULL
        GROUP BY "outbound_number_id";
    """
    return text, []


def clean_stale_bb_locks_query(threshold_minutes: int) -> Tuple[str, List[Any]]:
    """
    For ``clean_stale_bb_locks``: unlock rows where ``is_locked=TRUE`` and
    no update has happened in the threshold window. ``updated_at`` is the
    available lock-age proxy (no dedicated ``locked_at`` column today; the
    lock acquire updates ``updated_at`` so this is correct for stale-lock
    detection on BACKLOG rows).
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "is_locked" = FALSE, "updated_at" = NOW()
        WHERE "is_locked" = TRUE
          AND "status" = 'BACKLOG'
          AND "updated_at" < NOW() - ($1 || ' minutes')::interval
        RETURNING "id";
    """
    return text, [str(threshold_minutes)]


def update_lead_next_attempt_at_query(
    lead_id: str, next_attempt_at: datetime
) -> Tuple[str, List[Any]]:
    """
    For the manual ``/dispatch-now`` endpoint: bump ``next_attempt_at``.

    Guarded by ``is_locked = FALSE`` so a worker that locks the row between
    the handler's read and this UPDATE doesn't get its in-flight dispatch
    rewritten under it. Returns zero rows in that race; the handler maps
    that to a 409 retry.
    """
    text = f"""
        UPDATE "{LEAD_CALL_TRACKER_TABLE}"
        SET "next_attempt_at" = $2, "updated_at" = NOW()
        WHERE "id" = $1
          AND "status" = 'BACKLOG'
          AND "is_locked" = FALSE
        RETURNING *;
    """
    return text, [lead_id, next_attempt_at]
