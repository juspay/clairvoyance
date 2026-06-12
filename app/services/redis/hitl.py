"""Redis-backed Human-In-The-Loop (HITL) state management.

Stores pending HITL confirmations and tracks user approvals/rejections.
Used by the chat agent to pause tool execution pending user confirmation.

Storage shape:
- hitl:pending:{session_id}:{confirmation_id} -> JSON (pending confirmation)
- hitl:resolved:{session_id}:{confirmation_id} -> JSON (resolved confirmation)
- hitl:session:{session_id}:pending_id -> string (current pending confirmation_id for session)

Pending confirmations have TTL = timeout_seconds (auto-expire if no response).
Resolved confirmations have TTL = 1 hour (for debugging/audit).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.logger import logger

__all__ = [
    "HitlPendingInfo",
    "HitlResolvedInfo",
    "store_pending_hitl",
    "get_pending_hitl",
    "resolve_hitl",
    "get_resolved_hitl",
    "clear_pending_hitl",
    "get_session_pending_hitl",
]


# TTL for resolved confirmations (1 hour for debugging/audit)
RESOLVED_TTL_SECONDS = 3600


@dataclass
class HitlPendingInfo:
    """Info about a pending HITL confirmation."""

    confirmation_id: str
    session_id: str
    tool_name: str
    arguments: Dict[str, Any]
    created_at: str
    timeout_seconds: int
    tool_call_id: Optional[str] = None


@dataclass
class HitlResolvedInfo:
    """Info about a resolved HITL confirmation."""

    confirmation_id: str
    session_id: str
    approved: bool
    resolved_at: str
    # Pending info (copied at resolve time so it's available after pending key is deleted)
    tool_name: Optional[str] = None
    original_arguments: Optional[Dict[str, Any]] = None
    tool_call_id: Optional[str] = None


async def store_pending_hitl(
    session_id: str,
    confirmation_id: str,
    tool_name: str,
    arguments: Dict[str, Any],
    timeout_seconds: int,
    tool_call_id: Optional[str] = None,
) -> bool:
    """Store a pending HITL confirmation in Redis.

    Args:
        session_id: The chat session ID
        confirmation_id: Unique ID for this confirmation
        tool_name: Name of the tool requiring approval
        arguments: Tool arguments to show to user
        timeout_seconds: TTL for this pending confirmation
        tool_call_id: The LLM-generated tool_call_id for matching tool_results

    Returns:
        True if stored successfully, False otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            logger.warning("[HITL] Redis not configured, skipping pending store")
            return False

        redis = await get_redis_service()
        client = await redis.get_client()

        pending_key = f"hitl:pending:{session_id}:{confirmation_id}"
        session_key = f"hitl:session:{session_id}:pending_id"

        pending_data = {
            "confirmation_id": confirmation_id,
            "session_id": session_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "timeout_seconds": timeout_seconds,
            "tool_call_id": tool_call_id,
        }

        # Store pending confirmation with TTL
        # If timeout_seconds is None, use a default of 1 hour to prevent
        # indefinite accumulation of pending confirmations
        redis_ttl = timeout_seconds if timeout_seconds is not None else 3600
        await client.setex(
            pending_key,
            redis_ttl,
            json.dumps(pending_data),
        )

        # Store current pending ID for session (no TTL, overwritten on next pending)
        await client.set(session_key, confirmation_id)

        # Verify the session_key was set correctly
        verify = await client.get(session_key)
        verify_value = verify.decode() if isinstance(verify, bytes) else verify
        logger.info(
            f"[HITL] Stored pending confirmation {confirmation_id} "
            f"for tool {tool_name} in session {session_id} "
            f"(timeout={timeout_seconds}s) - session_key set to: {verify_value or 'NONE'}"
        )

        return True

    except Exception as e:
        logger.error(f"[HITL] Failed to store pending confirmation: {e}")
        return False


async def get_pending_hitl(
    session_id: str,
    confirmation_id: str,
) -> Optional[HitlPendingInfo]:
    """Get a pending HITL confirmation from Redis.

    Args:
        session_id: The chat session ID
        confirmation_id: The confirmation ID to look up

    Returns:
        HitlPendingInfo if found, None otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            return None

        redis = await get_redis_service()
        client = await redis.get_client()

        pending_key = f"hitl:pending:{session_id}:{confirmation_id}"
        raw = await client.get(pending_key)

        if not raw:
            return None

        data = json.loads(raw)
        return HitlPendingInfo(
            confirmation_id=data["confirmation_id"],
            session_id=data["session_id"],
            tool_name=data["tool_name"],
            arguments=data["arguments"],
            created_at=data["created_at"],
            timeout_seconds=data["timeout_seconds"],
        )

    except Exception as e:
        logger.error(f"[HITL] Failed to get pending confirmation: {e}")
        return None


async def resolve_hitl(
    session_id: str,
    confirmation_id: str,
    approved: bool,
) -> bool:
    """Mark a HITL confirmation as resolved (approved or rejected).

    Args:
        session_id: The chat session ID
        confirmation_id: The confirmation ID to resolve
        approved: Whether the user approved

    Returns:
        True if resolved successfully, False otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            logger.warning("[HITL] Redis not configured, skipping resolve")
            return False

        redis = await get_redis_service()
        client = await redis.get_client()

        pending_key = f"hitl:pending:{session_id}:{confirmation_id}"
        resolved_key = f"hitl:resolved:{session_id}:{confirmation_id}"
        session_key = f"hitl:session:{session_id}:pending_id"

        # Fetch pending info BEFORE deleting - needed to execute tool after resolution
        pending_raw = await client.get(pending_key)
        pending_info = None
        if pending_raw:
            pending_info = json.loads(pending_raw)

        # Store resolved confirmation for audit, including pending info
        # so _execute_pending_hitl_tool can use it after the pending key is deleted
        resolved_data = {
            "confirmation_id": confirmation_id,
            "session_id": session_id,
            "approved": approved,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            # Include pending info so it's available after pending key is deleted
            "tool_name": pending_info.get("tool_name") if pending_info else None,
            "original_arguments": (
                pending_info.get("arguments") if pending_info else None
            ),
            "tool_call_id": pending_info.get("tool_call_id") if pending_info else None,
        }

        await client.setex(
            resolved_key,
            RESOLVED_TTL_SECONDS,
            json.dumps(resolved_data),
        )

        # Delete pending confirmation, but keep the session's pending ID
        # until the user resumes the session turn and the approval is consumed.
        await client.delete(pending_key)

        # Verify session_key still has the pending ID
        session_pending = await client.get(session_key)
        if session_pending:
            session_pending_value = (
                session_pending.decode()
                if isinstance(session_pending, bytes)
                else session_pending
            )
            logger.info(
                f"[HITL] Session {session_id} still has pending_id {session_pending_value} "
                f"after resolving confirmation {confirmation_id}"
            )
        else:
            logger.warning(
                f"[HITL] Session {session_id} has NO pending_id after resolving "
                f"confirmation {confirmation_id} - this is the bug!"
            )

        logger.info(
            f"[HITL] Resolved confirmation {confirmation_id} "
            f"({'approved' if approved else 'rejected'}) in session {session_id}"
        )

        return True

    except Exception as e:
        logger.error(f"[HITL] Failed to resolve confirmation: {e}")
        return False


async def get_resolved_hitl(
    session_id: str,
    confirmation_id: str,
) -> Optional[HitlResolvedInfo]:
    """Get a resolved HITL confirmation from Redis.

    Args:
        session_id: The chat session ID
        confirmation_id: The confirmation ID to look up

    Returns:
        HitlResolvedInfo if found, None otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            return None

        redis = await get_redis_service()
        client = await redis.get_client()

        resolved_key = f"hitl:resolved:{session_id}:{confirmation_id}"
        raw = await client.get(resolved_key)

        if not raw:
            logger.debug(
                f"[HITL] No resolved HITL found for {confirmation_id} in session {session_id}"
            )
            return None

        data = json.loads(raw)
        logger.info(
            f"[HITL] Found resolved HITL {confirmation_id} in session {session_id}: "
            f"approved={data['approved']}, tool_name={data.get('tool_name')}"
        )
        return HitlResolvedInfo(
            confirmation_id=data["confirmation_id"],
            session_id=data["session_id"],
            approved=data["approved"],
            resolved_at=data["resolved_at"],
            tool_name=data.get("tool_name"),
            original_arguments=data.get("original_arguments"),
            tool_call_id=data.get("tool_call_id"),
        )

    except Exception as e:
        logger.error(f"[HITL] Failed to get resolved confirmation: {e}")
        return None


async def clear_pending_hitl(
    session_id: str,
    confirmation_id: str,
) -> bool:
    """Clear a pending HITL confirmation without resolving it.

    Args:
        session_id: The chat session ID
        confirmation_id: The confirmation ID to clear

    Returns:
        True if cleared successfully, False otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            return False

        redis = await get_redis_service()
        client = await redis.get_client()

        pending_key = f"hitl:pending:{session_id}:{confirmation_id}"
        session_key = f"hitl:session:{session_id}:pending_id"

        await client.delete(pending_key)

        # Clear session's pending ID if it matches
        current_pending = await client.get(session_key)
        current_pending_value = (
            current_pending.decode()
            if isinstance(current_pending, bytes)
            else current_pending
        )
        if current_pending_value and current_pending_value == confirmation_id:
            await client.delete(session_key)

        logger.info(
            f"[HITL] Cleared pending confirmation {confirmation_id} "
            f"in session {session_id}"
        )

        return True

    except Exception as e:
        logger.error(f"[HITL] Failed to clear pending confirmation: {e}")
        return False


async def get_session_pending_hitl(session_id: str) -> Optional[str]:
    """Get the current pending confirmation ID for a session.

    Args:
        session_id: The chat session ID

    Returns:
        Confirmation ID if there's a pending HITL, None otherwise
    """
    try:
        from app.services.redis import get_redis_service, is_redis_configured

        if not is_redis_configured():
            logger.debug("[HITL] Redis not configured in get_session_pending_hitl")
            return None

        redis = await get_redis_service()
        client = await redis.get_client()

        session_key = f"hitl:session:{session_id}:pending_id"
        raw = await client.get(session_key)

        if not raw:
            logger.debug(f"[HITL] No pending HITL found in session {session_id}")
            return None

        pending_id = raw.decode() if isinstance(raw, bytes) else raw
        logger.debug(f"[HITL] Found pending HITL {pending_id} in session {session_id}")
        return pending_id

    except Exception as e:
        logger.error(f"[HITL] Failed to get session pending: {e}")
        return None
