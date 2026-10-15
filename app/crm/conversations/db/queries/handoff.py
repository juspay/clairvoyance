"""SQL builders for crm_handoff — an agent asking for a person. One open per
thread (crm_handoff_merchant_open_uq); closing is one conditional UPDATE, so
a handoff closes once."""

from typing import Any, List, Tuple

HANDOFF_TABLE = "crm_handoff"

HANDOFF_COLUMNS = """
    id, merchant_id, conversation_id, chat_session_id, reason, summary,
    priority, claimed_by, claimed_at, outcome, closed_by, closed_at, opened_at
"""


def open_for_thread_query(merchant_id: str, thread_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {HANDOFF_COLUMNS}
          FROM {HANDOFF_TABLE}
         WHERE merchant_id = $1
           AND conversation_id = $2::uuid
           AND closed_at IS NULL
    """
    return query, [merchant_id, thread_id]


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
