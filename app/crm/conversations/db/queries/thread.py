"""SQL builders for crm_conversation — the thread. $n placeholders only; the
vocabulary is bound, never spelled."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

THREAD_TABLE = "crm_conversation"

THREAD_COLUMNS = """
    id, merchant_id, channel, contact_key, customer_id, address, binding_id,
    resolved_at, assignee_user_id, bot_template_id, bot_session_id,
    bot_cursor_at, last_inbound_at, last_message_at,
    preview, unread, assignment_trail, created_at, updated_at
"""


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


def thread_for_contact_query(
    merchant_id: str, channel: str, contact_key: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {THREAD_COLUMNS}
          FROM {THREAD_TABLE}
         WHERE merchant_id = $1 AND channel = $2 AND contact_key = $3
    """
    return query, [merchant_id, channel, contact_key]


def resolve_off_binding_query(
    merchant_id: str, channel: str, contact_key: str, binding_id: str
) -> Tuple[str, List[Any]]:
    """Her thread, still open on a binding Buddy has left, resolved quietly
    — what the move's own pass (resolve_binding_query) would have done, for
    a message to Buddy's binding that got here first (R7)."""
    query = f"""
        UPDATE {THREAD_TABLE}
           SET resolved_at = now(),
               assignee_user_id = NULL,
               bot_template_id = NULL,
               bot_session_id = NULL,
               unread = false
         WHERE merchant_id = $1
           AND channel = $2
           AND contact_key = $3
           AND binding_id <> $4::uuid
           AND resolved_at IS NULL
        RETURNING id
    """
    return query, [merchant_id, channel, contact_key, binding_id]


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
