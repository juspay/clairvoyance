"""SQL builders for the tool_approvals table (HITL approval gate).

This module contains ONLY query generation functions. Business logic lives
in accessor/breeze_buddy/tool_approvals.py. JSON serialization is the
caller's responsibility — query builders treat JSONB values as opaque
strings (matches the chat_session pattern).
"""

from typing import Any, List, Optional, Tuple

TOOL_APPROVALS_TABLE = "tool_approvals"

_APPROVAL_COLUMNS = """
    id, session_id, channel, tool_call_id, function_name,
    arguments, prompt, status, reason,
    requested_at, decided_at, expires_at
"""


def insert_tool_approval_query(
    session_id: str,
    tool_call_id: str,
    function_name: str,
    arguments_json: str,
    prompt: Optional[str],
    expiry_secs: float,
) -> Tuple[str, List[Any]]:
    """Insert a PENDING approval row, returning it.

    ``ON CONFLICT DO NOTHING`` so a duplicate provider tool_call_id can
    never crash the gate turn mid-stream (returns no row on conflict).
    """
    query = f"""
        INSERT INTO {TOOL_APPROVALS_TABLE} (
            session_id, channel, tool_call_id, function_name,
            arguments, prompt, expires_at
        )
        VALUES ($1, 'CHAT', $2, $3, $4::jsonb, $5,
                now() + make_interval(secs => $6))
        ON CONFLICT (session_id, tool_call_id) DO NOTHING
        RETURNING {_APPROVAL_COLUMNS}
    """
    return query, [
        session_id,
        tool_call_id,
        function_name,
        arguments_json,
        prompt,
        expiry_secs,
    ]


def get_tool_approval_query(
    session_id: str, tool_call_id: str
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_APPROVAL_COLUMNS}
        FROM {TOOL_APPROVALS_TABLE}
        WHERE session_id = $1 AND tool_call_id = $2
    """
    return query, [session_id, tool_call_id]


def list_pending_tool_approvals_query(
    session_id: str,
    include_expired: bool = False,
) -> Tuple[str, List[Any]]:
    """PENDING rows for a session, oldest first.

    ``include_expired=True`` is for the dangling-resolution sweep (which
    must see every PENDING row); the default filters lazily-expired rows
    out for user-facing surfaces (siblings check, widget resume state).
    """
    expiry_clause = "" if include_expired else "AND expires_at > now()"
    query = f"""
        SELECT {_APPROVAL_COLUMNS}
        FROM {TOOL_APPROVALS_TABLE}
        WHERE session_id = $1 AND status = 'PENDING' {expiry_clause}
        ORDER BY requested_at ASC
    """
    return query, [session_id]


def decide_tool_approval_query(
    session_id: str,
    tool_call_id: str,
    new_status: str,
    reason: Optional[str],
) -> Tuple[str, List[Any]]:
    """Atomic decision claim: only a PENDING row can be decided.

    Returns the updated row, or no row if the claim lost a race (the row
    was already decided/superseded/expired by another request).
    """
    query = f"""
        UPDATE {TOOL_APPROVALS_TABLE}
        SET status = $1, reason = $2, decided_at = now()
        WHERE session_id = $3 AND tool_call_id = $4 AND status = 'PENDING'
        RETURNING {_APPROVAL_COLUMNS}
    """
    return query, [new_status, reason, session_id, tool_call_id]


def claim_pending_tool_approvals_query(
    session_id: str,
    new_status: str,
    reason: Optional[str],
    only_expired: bool = False,
) -> Tuple[str, List[Any]]:
    """Bulk-claim PENDING rows (dangling resolution), returning them.

    - ``only_expired=False``: claims ALL pending rows (supersede on a new
      user message).
    - ``only_expired=True``: claims only rows past expires_at (lazy expiry
      inside the approval handler).
    """
    expiry_clause = "AND expires_at <= now()" if only_expired else ""
    query = f"""
        UPDATE {TOOL_APPROVALS_TABLE}
        SET status = $1, reason = $2, decided_at = now()
        WHERE session_id = $3 AND status = 'PENDING' {expiry_clause}
        RETURNING {_APPROVAL_COLUMNS}
    """
    return query, [new_status, reason, session_id]
