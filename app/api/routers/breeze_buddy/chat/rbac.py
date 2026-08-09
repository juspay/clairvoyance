"""
RBAC (Role-Based Access Control) utilities for chat sessions.
Handles reseller + merchant access control based on JWT token.

Mirrors the leads RBAC split: ``validate_chat_create_access`` returns
403 for the create path (caller already knows the reseller/template
scope), while ``validate_chat_session_access`` returns 404 for
operations on an existing session id (don't leak existence).
"""

from typing import Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.core.security.authorization import merchant_scope_permitted
from app.schemas import UserInfo
from app.schemas.breeze_buddy.chat import ChatSession


def validate_chat_create_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str],
    operation: str = "create chat session",
) -> None:
    """
    Validate user has access to create a chat session for the given
    reseller and merchant. Used by ``POST /chat/session`` where the
    caller has explicitly named the template (and therefore the
    reseller/merchant scope), so leaking the scope is fine.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_id: Merchant identifier to validate access for (optional)
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    if current_user.role == "admin":
        return

    if (
        reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} "
            f"for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )

    # Null merchant_id marks a reseller-scoped create: only reseller-role
    # accounts (or admin, above) may create it — never every merchant sharing the
    # reseller (PT-15). Mirrors validate_chat_session_access so the create path
    # can't be looser than the access path (which would otherwise let a merchant
    # persist a reseller-scoped session it then 404s on).
    if not merchant_scope_permitted(current_user, merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to {operation} "
            f"for unauthorized merchant: {merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access denied to merchant {merchant_id}"
                if merchant_id
                else "Access denied to reseller-scoped chat session"
            ),
        )


def validate_chat_session_access(
    current_user: UserInfo,
    session: ChatSession,
    operation: str = "access",
) -> None:
    """
    Validate user has access to read or operate on a specific chat
    session. Returns 404 (not 403) so unauthorized callers cannot
    discover whether a session exists by probing.

    Args:
        current_user: Current authenticated user
        session: ChatSession row to validate access against
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 404 if user lacks permission
    """
    if current_user.role == "admin":
        return

    if (
        session.reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} "
            f"chat session {session.id} for unauthorized reseller: "
            f"{session.reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
        )

    # Null merchant_id (reseller-scoped session) is reachable only by
    # reseller-role accounts or admin, not every merchant in the reseller (PT-15).
    if not merchant_scope_permitted(current_user, session.merchant_id):
        logger.warning(
            f"User {current_user.username} attempted to {operation} "
            f"chat session {session.id} for unauthorized merchant: "
            f"{session.merchant_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Chat session not found"
        )
