"""SQL builders for crm_handoff — an agent asking for a person. One open per
thread (crm_handoff_merchant_open_uq); closing is one conditional UPDATE, so
a handoff closes once."""

from datetime import datetime
from typing import Any, List, Optional, Tuple

HANDOFF_TABLE = "crm_handoff"
THREAD_TABLE = "crm_conversation"

HANDOFF_COLUMNS = """
    id, merchant_id, conversation_id, chat_session_id, reason, summary,
    priority, claimed_by, claimed_at, outcome, closed_by, closed_at, opened_at
"""


def open_handoff_query(
    merchant_id: str,
    thread_id: str,
    chat_session_id: str,
    reason: Optional[str],
    summary: Optional[str],
    priority: str,
) -> Tuple[str, List[Any]]:
    """Open one — or nothing, when the thread already has one open (the
    caller then reads that one: asking twice is the same ask)."""
    query = f"""
        INSERT INTO {HANDOFF_TABLE}
            (merchant_id, conversation_id, chat_session_id, reason, summary,
             priority)
        VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6)
        ON CONFLICT (merchant_id, conversation_id) WHERE closed_at IS NULL
        DO NOTHING
        RETURNING {HANDOFF_COLUMNS}
    """
    return query, [merchant_id, thread_id, chat_session_id, reason, summary, priority]


def open_for_thread_query(merchant_id: str, thread_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {HANDOFF_COLUMNS}
          FROM {HANDOFF_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND closed_at IS NULL
    """
    return query, [merchant_id, thread_id]


def open_for_threads_query(
    merchant_id: str, thread_ids: List[str]
) -> Tuple[str, List[Any]]:
    """The open handoff of each listed thread — one read for a page."""
    query = f"""
        SELECT {HANDOFF_COLUMNS}
          FROM {HANDOFF_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = ANY($2::uuid[])
           AND closed_at IS NULL
    """
    return query, [merchant_id, thread_ids]


def claim_query(
    merchant_id: str, thread_id: str, user_id: str
) -> Tuple[str, List[Any]]:
    """The teammate taking the thread claims its open handoff too."""
    query = f"""
        UPDATE {HANDOFF_TABLE}
           SET claimed_by = $3,
               claimed_at = now()
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND closed_at IS NULL
           AND claimed_at IS NULL
        RETURNING {HANDOFF_COLUMNS}
    """
    return query, [merchant_id, thread_id, user_id]


def close_query(
    merchant_id: str, thread_id: str, outcome: str, closed_by: Optional[str]
) -> Tuple[str, List[Any]]:
    """Close the thread's open handoff, once."""
    query = f"""
        UPDATE {HANDOFF_TABLE}
           SET outcome = $3,
               closed_by = $4,
               closed_at = now()
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND closed_at IS NULL
        RETURNING {HANDOFF_COLUMNS}
    """
    return query, [merchant_id, thread_id, outcome, closed_by]


def close_for_threads_query(
    merchant_id: str, thread_ids: List[str], outcome: str
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {HANDOFF_TABLE}
           SET outcome = $3,
               closed_at = now()
         WHERE merchant_id = $1
           AND conversation_id = ANY($2::uuid[])
           AND closed_at IS NULL
        RETURNING id
    """
    return query, [merchant_id, thread_ids, outcome]


def unclaimed_older_than_query(
    seconds: int, after: Optional[Tuple[datetime, str]], limit: int
) -> Tuple[str, List[Any]]:
    """Open handoffs nobody claimed for at least ``seconds``, oldest first
    (crm_handoff_unclaimed_ix), keyset-paged so a merchant with a long SLA
    never crowds out one whose handoff is due — the sweep reads each
    merchant's own SLA after this narrows."""
    after_at, after_id = after if after else (None, None)
    query = f"""
        SELECT {HANDOFF_COLUMNS}
          FROM {HANDOFF_TABLE}
         WHERE closed_at IS NULL
           AND claimed_at IS NULL
           AND opened_at < now() - make_interval(secs => $1::int)
           AND ($2::timestamptz IS NULL OR (opened_at, id) > ($2, $3::uuid))
         ORDER BY opened_at, id
         LIMIT $4
    """
    return query, [seconds, after_at, after_id, limit]
