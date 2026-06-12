"""Async accessors for tool_approvals (HITL approval gate).

Calls into queries/breeze_buddy/tool_approvals.py for SQL and
decoder/breeze_buddy/tool_approvals.py for row → Pydantic.
JSON serialization for JSONB columns happens here.
"""

import json
from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.tool_approvals import decode_tool_approval
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.tool_approvals import (
    claim_pending_tool_approvals_query,
    decide_tool_approval_query,
    get_tool_approval_query,
    insert_tool_approval_query,
    list_pending_tool_approvals_query,
)
from app.schemas.breeze_buddy.chat import ToolApproval, ToolApprovalStatus


async def insert_tool_approval(
    session_id: str,
    tool_call_id: str,
    function_name: str,
    arguments: Optional[Dict[str, Any]],
    prompt: Optional[str],
    expiry_secs: float,
) -> Optional[ToolApproval]:
    """Insert a PENDING approval row. Returns None on a duplicate
    (session_id, tool_call_id) — ON CONFLICT DO NOTHING."""
    query, values = insert_tool_approval_query(
        session_id=session_id,
        tool_call_id=tool_call_id,
        function_name=function_name,
        arguments_json=json.dumps(arguments or {}),
        prompt=prompt,
        expiry_secs=expiry_secs,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        if row is None:
            logger.warning(
                f"Duplicate tool approval ignored for session {session_id} "
                f"tool_call_id={tool_call_id}"
            )
            return None
        approval = decode_tool_approval(row)
        if approval:
            logger.info(
                f"Tool approval requested: session={session_id} "
                f"tool_call_id={tool_call_id} function={function_name} "
                f"expires_at={approval.expires_at.isoformat()}"
            )
        return approval
    except Exception as e:
        logger.error(
            f"Error inserting tool approval for session {session_id} "
            f"({function_name}): {e}"
        )
        raise


async def get_tool_approval(
    session_id: str, tool_call_id: str
) -> Optional[ToolApproval]:
    query, values = get_tool_approval_query(session_id, tool_call_id)
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        return decode_tool_approval(row) if row else None
    except Exception as e:
        logger.error(
            f"Error fetching tool approval {tool_call_id} for session "
            f"{session_id}: {e}"
        )
        raise


async def list_pending_tool_approvals(
    session_id: str,
    include_expired: bool = False,
) -> List[ToolApproval]:
    """PENDING rows oldest-first; excludes lazily-expired rows by default."""
    query, values = list_pending_tool_approvals_query(
        session_id, include_expired=include_expired
    )
    try:
        result = await run_parameterized_query(query, values)
        approvals = [decode_tool_approval(row) for row in result or []]
        return [a for a in approvals if a is not None]
    except Exception as e:
        logger.error(
            f"Error listing pending tool approvals for session {session_id}: {e}"
        )
        raise


async def decide_tool_approval(
    session_id: str,
    tool_call_id: str,
    new_status: ToolApprovalStatus,
    reason: Optional[str] = None,
) -> Optional[ToolApproval]:
    """Atomically claim a PENDING row with a decision.

    Returns the updated row, or None if the claim lost the race (the row
    was no longer PENDING) — callers map that to a 409.
    """
    query, values = decide_tool_approval_query(
        session_id=session_id,
        tool_call_id=tool_call_id,
        new_status=new_status.value,
        reason=reason,
    )
    try:
        result = await run_parameterized_query(query, values)
        row = result[0] if result else None
        approval = decode_tool_approval(row) if row else None
        if approval:
            logger.info(
                f"Tool approval decided: session={session_id} "
                f"tool_call_id={tool_call_id} "
                f"function={approval.function_name} -> {new_status.value}"
                + (f" ({reason})" if reason else "")
            )
        return approval
    except Exception as e:
        logger.error(
            f"Error deciding tool approval {tool_call_id} for session "
            f"{session_id}: {e}"
        )
        raise


async def claim_pending_tool_approvals(
    session_id: str,
    new_status: ToolApprovalStatus,
    reason: Optional[str] = None,
    only_expired: bool = False,
) -> List[ToolApproval]:
    """Bulk-claim PENDING rows (dangling resolution), returning them."""
    query, values = claim_pending_tool_approvals_query(
        session_id=session_id,
        new_status=new_status.value,
        reason=reason,
        only_expired=only_expired,
    )
    try:
        result = await run_parameterized_query(query, values)
        approvals = [decode_tool_approval(row) for row in result or []]
        claimed = [a for a in approvals if a is not None]
        if claimed:
            logger.info(
                f"Claimed {len(claimed)} pending tool approval(s) as "
                f"{new_status.value} for session {session_id}"
                + (" (expired only)" if only_expired else "")
            )
        return claimed
    except Exception as e:
        logger.error(
            f"Error claiming pending tool approvals for session " f"{session_id}: {e}"
        )
        raise
