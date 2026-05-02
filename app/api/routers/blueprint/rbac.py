"""
RBAC helper for Blueprint agent endpoints.
Reuses the existing Breeze Buddy RBAC token verification.
"""

from fastapi import Depends

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo


async def get_blueprint_user(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> UserInfo:
    """
    FastAPI dependency to get the current authenticated user for Blueprint endpoints.
    Reuses the existing Breeze Buddy RBAC authentication.
    """
    return current_user
