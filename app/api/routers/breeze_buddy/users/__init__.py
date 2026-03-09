"""
User Account API endpoints.

RBAC-controlled endpoints for managing user login accounts.
Separate from merchant entities - user accounts are login credentials with roles.
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.users import (
    DeleteUserResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)

from .handlers import (
    create_user_handler,
    delete_user_handler,
    get_all_users_handler,
    get_user_by_id_handler,
    update_user_handler,
)

router = APIRouter()


@router.post("/user", response_model=UserResponse, status_code=201)
async def create_user_account(
    user_data: UserCreate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a new user account (login account).

    **RBAC rules:**
    - Admin: Can create any role with any merchant_identifiers
    - Reseller: Can create user/merchant roles for their scoped merchant_identifiers
    - Merchant: Can create user roles for their own merchant identifiers

    Args:
        user_data: User account data (username, password, role, merchant_identifiers, etc.)

    Returns:
        Created user account with owner_id and timestamps

    Raises:
        400: If merchant_identifiers missing for merchant/user roles
        403: If user doesn't have permission to create this account type
        409: If username already exists
        500: If there's an error creating the user
    """
    return await create_user_handler(user_data, current_user)


@router.get("/users", response_model=UserListResponse)
async def list_users(
    page: int = Query(1, ge=1, description="Page number (1-indexed)"),
    limit: int = Query(50, ge=1, le=100, description="Items per page"),
    username: Optional[str] = Query(
        None, description="Filter by username (partial match)"
    ),
    role: Optional[str] = Query(None, description="Filter by role (exact match)"),
    reseller_id: Optional[str] = Query(
        None,
        description="Filter by reseller ID (checks if in reseller_ids array)",
    ),
    merchant_identifier: Optional[str] = Query(
        None,
        description="Filter by merchant identifier (checks if in merchant_identifiers array)",
    ),
    is_active: Optional[bool] = Query(None, description="Filter by active status"),
    sort_by: str = Query(
        "created_at",
        description="Sort by field",
        pattern="^(username|role|created_at|updated_at)$",
    ),
    sort_order: str = Query("desc", description="Sort order", pattern="^(asc|desc)$"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all user accounts with pagination and RBAC filtering.

    **RBAC rules:**
    - Admin: See all user accounts
    - Reseller: See only accounts with overlapping reseller_ids or merchant_identifiers
    - Merchant: See only accounts with overlapping merchant_identifiers

    Args:
        page: Page number (1-indexed)
        limit: Items per page (1-100)
        username: Filter by username (partial match, case-insensitive)
        role: Filter by role (admin, reseller, merchant, user)
        reseller_id: Filter by reseller ID (checks if in reseller_ids array)
        merchant_identifier: Filter by merchant identifier (checks if in merchant_identifiers array)
        is_active: Filter by active status
        sort_by: Sort field (username, role, created_at, updated_at)
        sort_order: Sort direction (asc, desc)

    Returns:
        Paginated list of user accounts

    Raises:
        403: If user doesn't have access
        500: If there's an error retrieving users
    """
    return await get_all_users_handler(
        page=page,
        limit=limit,
        username_filter=username,
        role_filter=role,
        reseller_id_filter=reseller_id,
        merchant_identifier_filter=merchant_identifier,
        is_active_filter=is_active,
        sort_by=sort_by,
        sort_order=sort_order,
        current_user=current_user,
    )


@router.get("/user/{user_id}", response_model=UserResponse)
async def get_user_account_by_id(
    user_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get user account by UUID.

    **RBAC rules:**
    - Admin: Can view any account
    - Reseller/Merchant: Can view accounts with overlapping merchant_identifiers or self
    - User: Can view only self

    Args:
        user_id: UUID of user account

    Returns:
        User account details

    Raises:
        403: If user doesn't have access
        404: If user not found
        500: If there's an error retrieving the user
    """
    return await get_user_by_id_handler(user_id, current_user)


@router.put("/user/{user_id}", response_model=UserResponse)
async def update_user_account(
    user_id: str,
    user_data: UserUpdate,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update user account by UUID.

    **RBAC rules:**
    - Admin: Can modify any account except other admins
    - Reseller: Can modify user/merchant accounts in their scope
    - Merchant: Can modify user accounts in their scope

    Note: username and role cannot be changed after creation.

    Args:
        user_id: UUID of user account
        user_data: Fields to update (password, email, merchant_identifiers, is_active)

    Returns:
        Updated user account

    Raises:
        400: If trying to remove merchant_identifiers from merchant/user accounts
        403: If user doesn't have permission to modify this account
        404: If user not found
        500: If there's an error updating the user
    """
    return await update_user_handler(user_id, user_data, current_user)


@router.delete("/user/{user_id}", response_model=DeleteUserResponse)
async def delete_user_account(
    user_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete user account by UUID.

    **RBAC rules:**
    - Admin accounts cannot be deleted by anyone (including admins themselves)
    - Admin: Can delete reseller/merchant/user accounts
    - Reseller: Can delete user/merchant accounts in their scope
    - Merchant: Can delete user accounts in their scope

    Note: Cannot delete your own account.

    Args:
        user_id: UUID of user account

    Returns:
        Deletion confirmation

    Raises:
        400: If trying to delete own account
        403: If user doesn't have permission to delete this account
        404: If user not found
        500: If there's an error deleting the user
    """
    return await delete_user_handler(user_id, current_user)
