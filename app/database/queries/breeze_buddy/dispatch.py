"""
Database queries for the event-driven dispatcher.

Distinct module so the dispatcher's needs don't bloat the main
``lead_call_tracker`` query file. All queries follow the project pattern:
``Tuple[str, List[Any]]`` of SQL text and parameterised values.
"""

from datetime import datetime
from typing import Any, List, Optional, Tuple

from app.database.queries.breeze_buddy.lead_call_tracker import (
    LEAD_CALL_TRACKER_TABLE,
)
from app.database.queries.breeze_buddy.telephony_number import (
    TELEPHONY_NUMBER_TABLE,
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
        SELECT id, reseller_id, EXTRACT(EPOCH FROM next_attempt_at) * 1000 AS score_ms,
               template_id
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = 'BACKLOG'
          AND "is_locked" = FALSE
          AND "execution_mode" IN ('TELEPHONY', 'TELEPHONY_TEST')
          AND "next_attempt_at" <= NOW() + ($1 || ' seconds')::interval
        ORDER BY "next_attempt_at" ASC
        LIMIT $2;
    """
    return text, [str(lookahead_seconds), limit]


def count_processing_by_telephony_number_query() -> Tuple[str, List[Any]]:
    """
    For ``reconcile_channel_tokens``: how many calls are HOLDING A CHANNEL on
    each telephony number right now? The reconciler compares this against
    LLEN of the channel LIST and tops up or trims to maintain
    ``M - in_flight == LLEN``.

    "Holding a channel" is not the same as "is a live call", and the
    difference is the whole reason this query joins. It must return exactly
    the set of leads that ``managers.calls._releases_capacity`` will hand a
    channel back for — the two are a matched pair, and any disagreement
    silently miscounts free capacity:

    - OUTBOUND: took one in ``_acquire_number`` before dialling, for every
      dispatchable execution mode.
    - INBOUND: only Plivo and Vobiz take one (``admit_inbound_call``). Exotel
      and Twilio inbound are ungated, so counting them would shrink the token
      stock for channels nobody actually took.

    Counting inbound at all is the fix for the churn this query used to
    cause: while inbound held channels, it reported 0 in-flight, the
    reconciler stocked Redis with M tokens for 0 free channels, and every
    outbound worker burned a token-acquire plus a DB round trip to be denied
    by ``_acquire_number`` and defer. Correct, but a hot loop precisely when
    the number was busiest. With inbound counted, the tokens are never minted
    and workers park on BLPOP instead.
    """
    text = f"""
        SELECT l."telephony_number_id", COUNT(*) AS in_flight
        FROM "{LEAD_CALL_TRACKER_TABLE}" l
        JOIN "{TELEPHONY_NUMBER_TABLE}" n ON n."id" = l."telephony_number_id"
        WHERE l."status" = 'PROCESSING'
          AND l."telephony_number_id" IS NOT NULL
          AND (
               (
                    l."call_direction" = 'OUTBOUND'
                AND l."execution_mode" IN ('TELEPHONY', 'TELEPHONY_TEST')
               )
            OR (
                    l."call_direction" = 'INBOUND'
                AND n."provider" IN ('PLIVO', 'VOBIZ')
               )
          )
        GROUP BY l."telephony_number_id";
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


# ---------------------------------------------------------------------------
# v2 event dialler (docs/dispatch-v2/design-card.md §5, §6b)
# ---------------------------------------------------------------------------


def get_due_backlog_page_query(
    after: Optional[Tuple[datetime, str]], limit: int, lookahead_seconds: int
) -> Tuple[str, List[Any]]:
    """
    For the v2 backlog reconciler: one keyset page of due BACKLOG, unlocked,
    dispatchable rows, ordered by ``(next_attempt_at, id)`` and starting after
    ``after`` (None = from the oldest). Paging by key, not OFFSET, lets the
    reconciler resume where it stopped instead of re-reading the oldest rows.
    """
    after_at, after_id = after if after is not None else (None, None)
    text = f"""
        SELECT "id", "template_id"::text AS template_id, "next_attempt_at",
               "meta_data" -> 'priority' AS priority
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = 'BACKLOG'
          AND "is_locked" = FALSE
          AND "execution_mode" IN ('TELEPHONY', 'TELEPHONY_TEST')
          AND "next_attempt_at" <= NOW() + make_interval(secs => $4::int)
          AND ($1::timestamptz IS NULL OR ("next_attempt_at", "id") > ($1::timestamptz, $2::text))
        ORDER BY "next_attempt_at", "id"
        LIMIT $3;
    """
    return text, [after_at, after_id, limit, lookahead_seconds]


def get_lead_dispatch_states_query(lead_ids: List[str]) -> Tuple[str, List[Any]]:
    """For the v2 ledger, lease reaper and prune: where each lead stands now."""
    text = f"""
        SELECT "id", "status", "is_locked", "template_id"::text AS template_id,
               "next_attempt_at", "meta_data" -> 'priority' AS priority
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "id" = ANY($1::text[]);
    """
    return text, [list(lead_ids)]


def _live_calls_predicate(number_param: str) -> str:
    """The calls holding a line on the numbers ``number_param`` selects: the set
    ``count_processing_by_telephony_number_query`` counts. PROCESSING outbound
    (dispatchable modes) and inbound calls."""
    return f"""
        {number_param}
          AND "status" = 'PROCESSING'
          AND (
               (
                    "call_direction" = 'OUTBOUND'
                AND "execution_mode" IN ('TELEPHONY', 'TELEPHONY_TEST')
               )
            OR "call_direction" = 'INBOUND'
          )"""


def get_live_calls_on_number_query(number_id: str) -> Tuple[str, List[Any]]:
    """For v2 seeding: the calls holding a line on one number."""
    text = f"""
        SELECT "id", "call_direction", "call_id"
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE {_live_calls_predicate('"telephony_number_id" = $1')};
    """
    return text, [number_id]


def get_live_calls_on_numbers_query(number_ids: List[str]) -> Tuple[str, List[Any]]:
    """For the v2 ledger: ``get_live_calls_on_number_query``'s set for many numbers in
    one statement, with the number of each row."""
    text = f"""
        SELECT "telephony_number_id", "id", "call_direction", "call_id"
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE {_live_calls_predicate('"telephony_number_id" = ANY($1::text[])')};
    """
    return text, [list(number_ids)]


def get_finished_inbound_calls_query(call_ids: List[str]) -> Tuple[str, List[Any]]:
    """For the v2 ledger: which of these inbound calls have ended."""
    text = f"""
        SELECT "call_id"
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "call_id" = ANY($1::text[])
          AND "call_direction" = 'INBOUND'
        GROUP BY "call_id"
        HAVING bool_and("status" = 'FINISHED');
    """
    return text, [list(call_ids)]


def get_known_inbound_calls_query(call_ids: List[str]) -> Tuple[str, List[Any]]:
    """For the v2 ledger: which of these inbound calls have a lead row at all."""
    text = f"""
        SELECT DISTINCT "call_id"
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "call_id" = ANY($1::text[])
          AND "call_direction" = 'INBOUND';
    """
    return text, [list(call_ids)]


def get_legacy_inflight_leads_query(lead_ids: List[str]) -> Tuple[str, List[Any]]:
    """
    For the v2 switch-on: BACKLOG leads today's dialler may be working on —
    every locked one, plus any of the given ids. The switch passes no ids: only
    a locked lead can be mid-dial past the worker's v2 redirect.
    """
    text = f"""
        SELECT "id", "template_id"::text AS template_id, "is_locked"
        FROM "{LEAD_CALL_TRACKER_TABLE}"
        WHERE "status" = 'BACKLOG'
          AND ("is_locked" = TRUE OR "id" = ANY($1::text[]));
    """
    return text, [list(lead_ids)]


def get_telephony_numbers_by_ids_query(number_ids: List[str]) -> Tuple[str, List[Any]]:
    """For the v2 switch and number-facts refresh."""
    text = f"""
        SELECT *
        FROM "{TELEPHONY_NUMBER_TABLE}"
        WHERE "id" = ANY($1::text[]);
    """
    return text, [list(number_ids)]


def set_telephony_number_channels_query(
    number_id: str, channels: int
) -> Tuple[str, List[Any]]:
    """
    For the v2 hand-back to today's dialler (the DB's own PROCESSING count)
    and the v2 channels mirror. Today's code only ever moves ``channels`` by
    +1/-1; this sets it outright.
    """
    text = f"""
        UPDATE "{TELEPHONY_NUMBER_TABLE}"
        SET "channels" = $2, "updated_at" = NOW()
        WHERE "id" = $1
        RETURNING "id";
    """
    return text, [number_id, channels]
