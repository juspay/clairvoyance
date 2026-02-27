"""
RBAC (Role-Based Access Control) utilities for blacklist management.
Only admin users can manage the blacklist.
"""

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def require_admin_access(
    current_user: UserInfo, operation: str = "perform this operation"
) -> None:
    """
    Validate user is an admin.

    Blacklist management is a system-wide operation that requires admin access.

    Args:
        current_user: Current authenticated user
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user is not admin
    """
    if current_user.role != "admin":
        logger.warning(
            f"Non-admin user {current_user.username} (role: {current_user.role}) "
            f"attempted to {operation}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin access required to {operation}",
        )
