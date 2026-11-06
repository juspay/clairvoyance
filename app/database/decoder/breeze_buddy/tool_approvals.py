"""Decoders for tool_approvals rows (HITL approval gate, migration 033)."""

from typing import Optional

import asyncpg

from app.schemas.breeze_buddy.chat import ToolApproval, ToolApprovalStatus
from app.utils.common import parse_json


def decode_tool_approval(row: asyncpg.Record) -> Optional[ToolApproval]:
    """Build a ToolApproval from a DB row, or None if row is empty."""
    if not row:
        return None
    arguments = parse_json(row, "arguments") or {}
    return ToolApproval(
        id=str(row["id"]),
        session_id=str(row["session_id"]),
        channel=row.get("channel") or "CHAT",
        tool_call_id=row["tool_call_id"],
        function_name=row["function_name"],
        arguments=arguments,
        prompt=row.get("prompt"),
        status=ToolApprovalStatus(row["status"]),
        reason=row.get("reason"),
        requested_at=row["requested_at"],
        decided_at=row.get("decided_at"),
        expires_at=row["expires_at"],
    )
