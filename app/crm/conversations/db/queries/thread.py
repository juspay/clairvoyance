"""SQL builders for crm_conversation — the thread. $n placeholders only; the
vocabulary is bound, never spelled."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

THREAD_TABLE = "crm_conversation"
HANDOFF_TABLE = "crm_handoff"

THREAD_COLUMNS = """
    id, merchant_id, channel, contact_key, customer_id, address, binding_id,
    resolved_at, assignee_user_id, bot_template_id, bot_session_id,
    bot_cursor_at, last_inbound_at, last_message_at,
    preview, unread, assignment_trail, created_at, updated_at
"""

#: "An open handoff exists for t" — the predicate the views share.
#: Correlated on the thread alias ``t``.
_OPEN_HANDOFF = f"""EXISTS (
    SELECT 1 FROM {HANDOFF_TABLE} h
     WHERE h.merchant_id = t.merchant_id
       AND h.conversation_id = t.id
       AND h.closed_at IS NULL
)"""


def ensure_thread_query(
    merchant_id: str,
    channel: str,
    contact_key: str,
    customer_id: Optional[str],
    address: Optional[str],
    binding_id: Optional[str],
) -> Tuple[str, List[Any]]:
    """The thread for (merchant, channel, contact), created on first sight
    and LOCKED for the projector's atom. A known customer id, address and
    binding overwrite the stored ones — the thread follows Buddy's binding."""
    query = f"""
        INSERT INTO {THREAD_TABLE} AS t
            (merchant_id, channel, contact_key, customer_id, address, binding_id)
        VALUES ($1, $2, $3, $4::uuid, $5, $6::uuid)
        ON CONFLICT (merchant_id, channel, contact_key) DO UPDATE
           SET customer_id = COALESCE(EXCLUDED.customer_id, t.customer_id),
               address = COALESCE(EXCLUDED.address, t.address),
               binding_id = COALESCE(EXCLUDED.binding_id, t.binding_id)
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, channel, contact_key, customer_id, address, binding_id]


def thread_query(
    merchant_id: str, thread_id: str, for_update: bool = False
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {THREAD_COLUMNS}
          FROM {THREAD_TABLE}
         WHERE merchant_id = $1
           AND id = $2::uuid
        {"FOR UPDATE" if for_update else ""}
    """
    return query, [merchant_id, thread_id]


def threads_by_ids_query(
    merchant_id: str, thread_ids: List[str]
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {THREAD_COLUMNS}
          FROM {THREAD_TABLE}
         WHERE merchant_id = $1
           AND id = ANY($2::uuid[])
    """
    return query, [merchant_id, thread_ids]


def thread_for_contact_query(
    merchant_id: str, channel: str, contact_key: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {THREAD_COLUMNS}
          FROM {THREAD_TABLE}
         WHERE merchant_id = $1 AND channel = $2 AND contact_key = $3
    """
    return query, [merchant_id, channel, contact_key]


def record_inbound_query(
    merchant_id: str,
    thread_id: str,
    occurred_at: datetime,
    preview: Optional[str],
    reopen: bool,
    bot_template_id: Optional[str],
    bot_session_id: Optional[str],
    written_at: datetime,
) -> Tuple[str, List[Any]]:
    """A customer's message landed: the window moves (forward only — a late
    letter never shortens it), the list row updates, and the controller
    fields take the plan's values. Reopening clears resolved_at and puts
    Buddy's cursor just before her message (``written_at``, its created_at,
    which rises per thread in microseconds): Buddy answers this message only,
    never what the closed conversation held."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET last_inbound_at = GREATEST(COALESCE(last_inbound_at, $3), $3),
               last_message_at = GREATEST(last_message_at, $3),
               preview = COALESCE($4, preview),
               unread = true,
               resolved_at = CASE WHEN $5 THEN NULL ELSE resolved_at END,
               bot_cursor_at = CASE WHEN $5 THEN $8::timestamptz - interval '1 microsecond'
                                    ELSE bot_cursor_at END,
               bot_template_id = $6::uuid,
               bot_session_id = $7::uuid
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {THREAD_COLUMNS}
    """
    return query, [
        merchant_id,
        thread_id,
        occurred_at,
        preview,
        reopen,
        bot_template_id,
        bot_session_id,
        written_at,
    ]


def touch_outbound_query(
    merchant_id: str, thread_id: str, occurred_at: datetime, preview: Optional[str]
) -> Tuple[str, List[Any]]:
    """We wrote to her: the list row moves, the window does not."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET last_message_at = GREATEST(last_message_at, $3),
               preview = COALESCE($4, preview)
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, occurred_at, preview]


# ---------------------------------------------------------------------------
# the teammate's actions
# ---------------------------------------------------------------------------


def take_over_query(
    merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """Take over: compare-and-set on the assignee — the first teammate wins,
    a second gets no row. Buddy lets go in the same statement."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET assignee_user_id = $3,
               bot_template_id = NULL,
               bot_session_id = NULL,
               unread = false,
               assignment_trail = assignment_trail || $4::jsonb
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND assignee_user_id IS NULL
           AND resolved_at IS NULL
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, user_id, json.dumps([trail])]


def assign_query(
    merchant_id: str, thread_id: str, user_id: str, trail: Dict[str, Any]
) -> Tuple[str, List[Any]]:
    """A manager hands the thread to a teammate, whoever held it."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET assignee_user_id = $3,
               bot_template_id = NULL,
               bot_session_id = NULL,
               assignment_trail = assignment_trail || $4::jsonb
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND resolved_at IS NULL
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, user_id, json.dumps([trail])]


def hand_back_query(
    merchant_id: str,
    thread_id: str,
    user_id: str,
    agent_id: Optional[str],
    trail: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Back to Buddy: only the assignee may, and Buddy answers what she says
    NEXT — the cursor moves past her last message, so nothing the teammate
    already handled is answered twice. Her messages are stamped under this
    same row lock (insert_inbound_query), so the next one lands after it."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET assignee_user_id = NULL,
               bot_template_id = $4::uuid,
               bot_session_id = NULL,
               bot_cursor_at = clock_timestamp(),
               assignment_trail = assignment_trail || $5::jsonb
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND assignee_user_id = $3
           AND resolved_at IS NULL
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, user_id, agent_id, json.dumps([trail])]


def resolve_query(
    merchant_id: str, thread_id: str, last_inbound_at: Optional[datetime] = None
) -> Tuple[str, List[Any]]:
    """Done: nobody holds it any more. Her next message reopens it.

    With ``last_inbound_at`` (the sweeps), only if she hasn't written since
    the caller read the thread: a message that arrived meanwhile started a
    new window, and resolving would bury it."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET resolved_at = now(),
               assignee_user_id = NULL,
               bot_template_id = NULL,
               bot_session_id = NULL,
               unread = false
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND resolved_at IS NULL
           AND ($3::timestamptz IS NULL OR last_inbound_at = $3)
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, last_inbound_at]


def resolve_binding_query(merchant_id: str, binding_id: str) -> Tuple[str, List[Any]]:
    """Buddy moved off this binding: every open thread on it is resolved
    quietly, in one statement (R7)."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET resolved_at = now(),
               assignee_user_id = NULL,
               bot_template_id = NULL,
               bot_session_id = NULL,
               unread = false
         WHERE merchant_id = $1
           AND binding_id = $2::uuid
           AND resolved_at IS NULL
        RETURNING id
    """
    return query, [merchant_id, binding_id]


def mark_read_query(merchant_id: str, thread_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {THREAD_TABLE}
           SET unread = false
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id]


# ---------------------------------------------------------------------------
# the Inbox list
# ---------------------------------------------------------------------------

#: Each view's predicate over ``t``. ``me.user_id`` is the asking user,
#: bound ONCE as $3 in the ``me`` CTE so every view types it (a parameter
#: only "mine" referenced would be untyped in the others).
VIEW_PREDICATES: Dict[str, str] = {
    "needs_attention": f"t.resolved_at IS NULL AND t.assignee_user_id IS NULL AND {_OPEN_HANDOFF}",
    "mine": "t.resolved_at IS NULL AND t.assignee_user_id = me.user_id",
    "buddy": (
        "t.resolved_at IS NULL AND t.assignee_user_id IS NULL "
        f"AND t.bot_template_id IS NOT NULL AND NOT {_OPEN_HANDOFF}"
    ),
    "unassigned": (
        "t.resolved_at IS NULL AND t.assignee_user_id IS NULL "
        f"AND t.bot_template_id IS NULL AND NOT {_OPEN_HANDOFF}"
    ),
    "resolved": "t.resolved_at IS NOT NULL",
    "all": "true",
}


def list_threads_query(
    merchant_id: str,
    view: str,
    user_id: str,
    channel: Optional[str],
    search: Optional[str],
    before: Optional[Tuple[datetime, str]],
    limit: int,
) -> Tuple[str, List[Any]]:
    """One page of a view, newest first, keyset-paged on (last_message_at,
    id) so a busy Inbox never re-reads what it already showed. ``search``
    matches the customer's address or the preview."""
    predicate = VIEW_PREDICATES[view]
    before_at, before_id = before if before else (None, None)
    query = f"""
        WITH me AS (SELECT $3::text AS user_id)
        SELECT {", ".join("t." + c.strip() for c in THREAD_COLUMNS.split(","))}
          FROM {THREAD_TABLE} t, me
         WHERE t.merchant_id = $1
           AND {predicate}
           AND ($2::text IS NULL OR t.channel = $2)
           AND ($4::text IS NULL OR t.address ILIKE '%' || $4 || '%'
                OR t.preview ILIKE '%' || $4 || '%')
           AND ($5::timestamptz IS NULL
                OR (t.last_message_at, t.id) < ($5, $6::uuid))
         ORDER BY t.last_message_at DESC, t.id DESC
         LIMIT $7
    """
    return query, [merchant_id, channel, user_id, search, before_at, before_id, limit]


#: The views counted on every list read — the open ones; counting resolved
#: would scan the merchant's whole history.
COUNTED_VIEWS = ("needs_attention", "mine", "buddy", "unassigned")


def view_counts_query(
    merchant_id: str, user_id: str, channel: Optional[str]
) -> Tuple[str, List[Any]]:
    """How many threads each open view holds — one pass over the merchant's
    open threads, with the same channel filter as the list."""
    filters = ",\n".join(
        f"count(*) FILTER (WHERE {VIEW_PREDICATES[view]}) AS {view}"
        for view in COUNTED_VIEWS
    )
    query = f"""
        WITH me AS (SELECT $3::text AS user_id)
        SELECT {filters}
          FROM {THREAD_TABLE} t, me
         WHERE t.merchant_id = $1
           AND t.resolved_at IS NULL
           AND ($2::text IS NULL OR t.channel = $2)
    """
    return query, [merchant_id, channel, user_id]


# ---------------------------------------------------------------------------
# Buddy's turns: the per-thread writes
# ---------------------------------------------------------------------------


def set_bot_session_query(
    merchant_id: str, thread_id: str, session_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {THREAD_TABLE}
           SET bot_session_id = $3::uuid
         WHERE merchant_id = $1
           AND id = $2::uuid
           AND bot_template_id IS NOT NULL
           AND resolved_at IS NULL
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, session_id]


def mark_bot_cursor_query(
    merchant_id: str, thread_id: str, answered_upto: datetime
) -> Tuple[str, List[Any]]:
    """The turn answered everything up to ``answered_upto``. Forward only."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET bot_cursor_at = GREATEST(COALESCE(bot_cursor_at, $3), $3)
         WHERE merchant_id = $1
           AND id = $2::uuid
        RETURNING {THREAD_COLUMNS}
    """
    return query, [merchant_id, thread_id, answered_upto]


# ---------------------------------------------------------------------------
# the sweeps (cross-tenant)
# ---------------------------------------------------------------------------


def closing_candidates_query(
    channel: str,
    older_than_seconds: int,
    after: Optional[Tuple[datetime, str]],
    limit: int,
) -> Tuple[str, List[Any]]:
    """Open threads on a channel whose window is within the longest lead of
    shutting (or already shut), oldest first, keyset-paged so threads that
    must wait never crowd out the ones due. The per-thread decision reads the
    merchant's own lead; this only narrows (crm_conversation_window_ix)."""
    after_at, after_id = after if after else (None, None)
    query = f"""
        SELECT {THREAD_COLUMNS}
          FROM {THREAD_TABLE}
         WHERE resolved_at IS NULL
           AND last_inbound_at IS NOT NULL
           AND last_inbound_at < now() - make_interval(secs => $2::int)
           AND channel = $1
           AND ($3::timestamptz IS NULL OR (last_inbound_at, id) > ($3, $4::uuid))
         ORDER BY last_inbound_at, id
         LIMIT $5
    """
    return query, [channel, older_than_seconds, after_at, after_id, limit]


def delete_resolved_query(older_than_days: int, limit: int) -> Tuple[str, List[Any]]:
    """Retention (D22): resolved threads older than the window go, their
    timeline and handoffs with them (ON DELETE CASCADE). Batched. The age is
    checked again on the row the delete reaches: a delete that waits on her
    reopening the thread re-checks only its own WHERE, not the subquery."""
    query = f"""
        WITH gone AS (
            DELETE FROM {THREAD_TABLE}
             WHERE id IN (
                       SELECT id FROM {THREAD_TABLE}
                        WHERE resolved_at IS NOT NULL
                          AND resolved_at < now() - make_interval(days => $1::int)
                        LIMIT $2
                   )
               AND resolved_at < now() - make_interval(days => $1::int)
            RETURNING 1
        )
        SELECT count(*) FROM gone
    """
    return query, [older_than_days, limit]
