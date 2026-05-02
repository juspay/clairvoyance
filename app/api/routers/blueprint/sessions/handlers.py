"""
Business logic handlers for blueprint session operations.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.blueprint.sessions import (
    create_session,
    delete_session,
    get_session_by_id,
    get_sessions_by_user,
)
from app.schemas import UserInfo
from app.schemas.blueprint.session import (
    BlueprintMode,
    CreateSessionRequest,
    CreateSessionResponse,
    SessionListResponse,
)


async def create_session_handler(
    request: CreateSessionRequest,
    current_user: UserInfo,
) -> CreateSessionResponse:
    """
    Create a new blueprint session.

    Args:
        request: Session creation request
        current_user: Current authenticated user

    Returns:
        CreateSessionResponse with session details

    Raises:
        HTTPException: 400 if edit mode without template_id, 500 on error
    """
    try:
        # Validate edit mode requires template_id
        if request.mode == BlueprintMode.EDIT and not request.template_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="template_id is required when mode is 'edit'",
            )

        # Resolve identity from auth context. Non-admin users:
        #   - reseller_id auto-picks the single value when they have one;
        #     must be in their authorized list otherwise.
        #   - merchant_id either auto-picks or must be in their list.
        # Admin users may pass any value.
        reseller_id = _resolve_reseller_id(request.reseller_id, current_user)
        merchant_id = _resolve_merchant_id(request.merchant_id, current_user)

        logger.info(
            f"User {current_user.username} creating blueprint session "
            f"(mode: {request.mode}, reseller: {reseller_id}, "
            f"merchant: {merchant_id or '-'})"
        )

        now = datetime.now(timezone.utc)
        session_id = str(uuid4())
        langgraph_thread_id = str(uuid4())

        session = await create_session(
            session_id=session_id,
            user_id=current_user.id,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            mode=request.mode.value,
            template_id=request.template_id,
            langgraph_thread_id=langgraph_thread_id,
            current_step=None,
            status="active",
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(days=7),
        )

        if not session:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create blueprint session",
            )

        logger.info(f"Blueprint session created: {session.id}")

        return CreateSessionResponse(
            session_id=session.id,
            langgraph_thread_id=session.langgraph_thread_id,
            mode=session.mode,
            status=session.status,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating blueprint session: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating blueprint session: {str(e)}",
        )


def _resolve_reseller_id(requested: Optional[str], current_user: UserInfo) -> str:
    """Return the reseller_id to use for this session, enforcing auth scope.

    * Non-admin user with no request → use their single ``reseller_ids[0]``;
      400 if they have zero or multiple resellers (must specify).
    * Non-admin user with a request → must match one of their authorized
      ``reseller_ids``; 403 otherwise.
    * Admin user with a request → use it as-is.
    * Admin user with no request → 400; admins must explicitly choose.
    """
    is_admin = getattr(current_user.role, "value", str(current_user.role)) == "admin"
    user_resellers = list(current_user.reseller_ids or [])

    if requested:
        if is_admin or requested in user_resellers:
            return requested
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"User {current_user.username} is not authorized for "
                f"reseller {requested!r}."
            ),
        )

    if is_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Admins must specify reseller_id explicitly.",
        )
    if len(user_resellers) == 1:
        return user_resellers[0]
    if not user_resellers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"User {current_user.username} has no reseller assignments — "
                "ask an admin to grant one before creating a template."
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=("User has multiple resellers; specify reseller_id in the request."),
    )


def _resolve_merchant_id(
    requested: Optional[str], current_user: UserInfo
) -> Optional[str]:
    """Return the merchant_id to use for this session (always optional).

    Admins pass through. Non-admins are restricted to their list when they
    pass one; an unset request stays unset (the template can be merchant-
    agnostic).
    """
    if not requested:
        return None
    is_admin = getattr(current_user.role, "value", str(current_user.role)) == "admin"
    user_merchants = list(current_user.merchant_ids or [])
    if is_admin or requested in user_merchants:
        return requested
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"User {current_user.username} is not authorized for "
            f"merchant {requested!r}."
        ),
    )


async def list_sessions_handler(
    current_user: UserInfo,
    status_filter: Optional[str] = None,
) -> SessionListResponse:
    """
    List blueprint sessions for the current user.

    Args:
        current_user: Current authenticated user
        status_filter: Optional status filter

    Returns:
        SessionListResponse with sessions list and total count
    """
    logger.info(
        f"User {current_user.username} listing blueprint sessions "
        f"(status: {status_filter})"
    )

    try:
        sessions = await get_sessions_by_user(current_user.id, status_filter)

        return SessionListResponse(
            sessions=sessions,
            total=len(sessions),
        )

    except Exception as e:
        logger.error(f"Error listing blueprint sessions: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing blueprint sessions: {str(e)}",
        )


async def get_session_handler(
    session_id: str,
    current_user: UserInfo,
):
    """
    Get a blueprint session by ID.

    Args:
        session_id: Session UUID
        current_user: Current authenticated user

    Returns:
        BlueprintSessionModel

    Raises:
        HTTPException: 404 if not found, 403 if not owner
    """
    logger.info(
        f"User {current_user.username} requesting blueprint session: {session_id}"
    )

    try:
        session = await get_session_by_id(session_id)

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Blueprint session not found: {session_id}",
            )

        # Verify ownership (admin can access any session)
        if current_user.role != "admin" and session.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this blueprint session",
            )

        return session

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting blueprint session: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting blueprint session: {str(e)}",
        )


async def delete_session_handler(
    session_id: str,
    current_user: UserInfo,
):
    """
    Delete a blueprint session.

    Args:
        session_id: Session UUID
        current_user: Current authenticated user

    Returns:
        Success message

    Raises:
        HTTPException: 404 if not found, 403 if not owner
    """
    logger.info(
        f"User {current_user.username} deleting blueprint session: {session_id}"
    )

    try:
        # Verify session exists and user has access
        session = await get_session_by_id(session_id)

        if not session:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Blueprint session not found: {session_id}",
            )

        # Verify ownership (admin can delete any session)
        if current_user.role != "admin" and session.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this blueprint session",
            )

        deleted = await delete_session(session_id)

        if not deleted:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to delete blueprint session",
            )

        return {
            "status": "success",
            "message": f"Blueprint session '{session_id}' deleted successfully",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting blueprint session: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error deleting blueprint session: {str(e)}",
        )
