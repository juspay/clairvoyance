"""
RBAC (Role-Based Access Control) utilities for outbound numbers.
Only admin users can manage outbound numbers.
"""

from typing import List

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def require_admin_access(
    current_user: UserInfo, operation: str = "perform this operation"
) -> None:
    """
    Validate user is an admin.

    Outbound numbers are system-wide resources that require admin access.

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


def filter_numbers_by_rbac(numbers: List, current_user: UserInfo) -> List:
    """
    Filter outbound numbers based on user's RBAC permissions.

    Currently all authenticated users can view outbound numbers,
    but only admins can create/update/delete.

    Args:
        numbers: List of outbound number objects
        current_user: Current authenticated user

    Returns:
        List of outbound numbers (currently returns all for read operations)
    """
    # All authenticated users can view numbers
    # (numbers are needed to understand call routing)
    return numbers
