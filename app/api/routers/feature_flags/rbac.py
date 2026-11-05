"""RBAC dependencies for feature flags."""

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.security.breeze_buddy.rbac_token import rbac_token_manager
from app.schemas import UserInfo

security = HTTPBearer()


async def get_current_user_with_role(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserInfo:
    if not credentials:
        raise HTTPException(
            status_code=401,
            detail="Authorization header missing",
        )
    return rbac_token_manager.verify_rbac_token(credentials.credentials)


def require_admin(current_user: UserInfo) -> None:
    """Admin-only to prevent unauthorized config changes"""
    if current_user.role.value != "admin":
        raise HTTPException(
            status_code=403, detail="Only admins can modify feature flags"
        )
