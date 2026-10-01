"""SQL builders for crm_conversation_message — the timeline. The partial
uniques on event_raw_id and message_id make every projector write a replay
no-op (ON CONFLICT ... DO NOTHING)."""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

TIMELINE_TABLE = "crm_conversation_message"

TIMELINE_COLUMNS = """
    id, conversation_id, kind, author_kind, author_user_id, event_raw_id,
    message_id, provider_message_id, body, occurred_at
"""


def _body(body: Optional[Dict[str, Any]]) -> Optional[str]:
    return json.dumps(body) if body is not None else None


def insert_inbound_query(
    merchant_id: str,
    thread_id: str,
    kind: str,
    author_kind: str,
    event_raw_id: Optional[str],
    provider_message_id: Optional[str],
    body: Optional[Dict[str, Any]],
    occurred_at: datetime,
) -> Tuple[str, List[Any]]:
    """What the customer said. Keyed on its letter: a replayed letter writes
    nothing (no row comes back)."""
    query = f"""
        INSERT INTO {TIMELINE_TABLE}
            (merchant_id, conversation_id, kind, author_kind, event_raw_id,
             provider_message_id, body, occurred_at)
        VALUES ($1, $2::uuid, $3, $4, $5::uuid, $6, $7::jsonb, $8)
        ON CONFLICT (merchant_id, event_raw_id) WHERE event_raw_id IS NOT NULL
        DO NOTHING
        RETURNING {TIMELINE_COLUMNS}
    """
    return query, [
        merchant_id,
        thread_id,
        kind,
        author_kind,
        event_raw_id,
        provider_message_id,
        _body(body),
        occurred_at,
    ]


def insert_outbound_query(
    merchant_id: str,
    thread_id: str,
    kind: str,
    author_kind: str,
    author_user_id: Optional[str],
    message_id: Optional[str],
    provider_message_id: Optional[str],
    body: Optional[Dict[str, Any]],
    occurred_at: datetime,
) -> Tuple[str, List[Any]]:
    """What we said (or a teammate's note, which has no manifest row). Keyed
    on its manifest row: the writer inserts it at once (read-your-writes) and
    the projector's later pass over the same send is a no-op."""
    query = f"""
        INSERT INTO {TIMELINE_TABLE}
            (merchant_id, conversation_id, kind, author_kind, author_user_id,
             message_id, provider_message_id, body, occurred_at)
        VALUES ($1, $2::uuid, $3, $4, $5, $6::uuid, $7, $8::jsonb, $9)
        ON CONFLICT (merchant_id, message_id) WHERE message_id IS NOT NULL
        DO NOTHING
        RETURNING {TIMELINE_COLUMNS}
    """
    return query, [
        merchant_id,
        thread_id,
        kind,
        author_kind,
        author_user_id,
        message_id,
        provider_message_id,
        _body(body),
        occurred_at,
    ]


def timeline_query(
    merchant_id: str,
    thread_id: str,
    before: Optional[Tuple[datetime, str]],
    limit: int,
) -> Tuple[str, List[Any]]:
    """One page of the thread, newest first, keyset-paged."""
    before_at, before_id = before if before else (None, None)
    query = f"""
        SELECT {TIMELINE_COLUMNS}
          FROM {TIMELINE_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND ($3::timestamptz IS NULL OR (occurred_at, id) < ($3, $4::uuid))
         ORDER BY occurred_at DESC, id DESC
         LIMIT $5
    """
    return query, [merchant_id, thread_id, before_at, before_id, limit]


def rows_after_query(
    merchant_id: str,
    thread_id: str,
    kinds: List[str],
    after: Optional[datetime],
    limit: int,
) -> Tuple[str, List[Any]]:
    """The thread's rows of these kinds after a moment, oldest first — what
    Buddy has not answered yet, or what a widget has not shown yet."""
    query = f"""
        SELECT {TIMELINE_COLUMNS}
          FROM {TIMELINE_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND kind = ANY($3::text[])
           AND ($4::timestamptz IS NULL OR occurred_at > $4)
         ORDER BY occurred_at, id
         LIMIT $5
    """
    return query, [merchant_id, thread_id, kinds, after, limit]


def last_by_author_query(
    merchant_id: str, thread_id: str, kind: str, author_kind: str
) -> Tuple[str, List[Any]]:
    """When this author last wrote on the thread — a teammate's last reply
    decides whether the closing message goes out for them (D9)."""
    query = f"""
        SELECT max(occurred_at) AS at
          FROM {TIMELINE_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND kind = $3
           AND author_kind = $4
    """
    return query, [merchant_id, thread_id, kind, author_kind]
