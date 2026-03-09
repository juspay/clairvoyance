"""
Handlers for user account management endpoints.
Implements business logic and RBAC for user login accounts.

RBAC rules for account creation:
- Admin: Can create reseller, merchant, user with any merchant_ids
- Reseller: Can create merchant/user with subset of own merchant_ids; becomes owner_id
- Merchant: Can create user with only own merchant_id(s); becomes owner_id
- User: Cannot create accounts
"""

import uuid
from typing import List, Optional

import asyncpg
from fastapi import HTTPException

from app.core.logger import logger
from app.core.security.authorization import (
    resolve_merchant_ids,
    validate_merchant_ids_subset,
)
from app.database.accessor.breeze_buddy import users as user_accessors
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_reseller
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.users import (
    DeleteUserResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)


async def _get_reseller_merchants(reseller_id: str) -> List[str]:
    """Get all merchant_identifiers for a given reseller_id.

    Args:
        reseller_id: The reseller ID to look up

    Returns:
        List of merchant_identifiers for that reseller
    """

    merchants, _ = await get_merchants_by_reseller(reseller_id)
    return [m.merchant_identifier for m in merchants]


async def _check_create_access(
    current_user: UserInfo,
    target_role: UserRole,
    reseller_ids: List[str],
    merchant_identifiers: List[str],
):
    """
    Check if user can create accounts with target role and access identifiers.

    Validation flow:
    1. First validate reseller_ids (user must have access to those resellers).
    2. Then validate merchant_identifiers against what those resellers have.
    Rules:
    - Admin: Can create any role with any identifiers
    - Reseller: Can create merchant/user roles only
    - Merchant: Can create user roles only
    - User: Cannot create accounts
    """
    if current_user.role == UserRole.ADMIN:
        return  # Admin can create anything

    if current_user.role == UserRole.RESELLER:
        # Reseller can only create user/merchant roles
        if target_role not in {UserRole.MERCHANT, UserRole.USER}:
            raise HTTPException(
                status_code=403,
                detail="Resellers can only create merchant/user accounts",
            )

        # Step 1: Validate reseller_ids are within current user's scope
        # Reseller with wildcard reseller_ids can assign any reseller_ids in their scope
        if "*" not in current_user.reseller_ids:
            for rid in reseller_ids:
                if rid not in current_user.reseller_ids:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Cannot create accounts for reseller '{rid}' outside your scope",
                    )

        # Step 2: Validate merchant_identifiers
        # If wildcard ["*"], grant access to all merchants of the specified resellers
        # Otherwise, validate specific merchants are allowed
        if merchant_identifiers and merchant_identifiers != ["*"]:
            # Get all merchants under the specified resellers
            allowed_merchants = []
            for rid in reseller_ids:
                reseller_merchants = await _get_reseller_merchants(rid)
                allowed_merchants.extend(reseller_merchants)

            # Validate all requested merchants are in the allowed list
            invalid_merchants = [
                m for m in merchant_identifiers if m not in allowed_merchants
            ]
            if invalid_merchants:
                raise HTTPException(
                    status_code=403,
                    detail=f"Merchant(s) {invalid_merchants} not found under specified resellers",
                )

        return

    if current_user.role == UserRole.MERCHANT:
        # Merchant can only create user accounts
        if target_role != UserRole.USER:
            raise HTTPException(
                status_code=403, detail="Merchants can only create user accounts"
            )

        # Validate merchant_identifiers are within merchant's scope
        # If merchant has wildcard merchant_identifiers, they can assign wildcard to users
        if "*" in current_user.merchant_identifiers:
            # Merchant with wildcard can assign specific merchants or wildcard
            return

        allowed = await resolve_merchant_ids(current_user)
        validate_merchant_ids_subset(
            merchant_identifiers,
            allowed,
            "Can only create user accounts for your own merchant identifiers",
        )
        return

    # User role cannot create accounts
    raise HTTPException(
        status_code=403, detail="Insufficient permissions to create accounts"
    )


async def _check_update_delete_access(
    current_user: UserInfo, target_user: UserResponse
) -> Optional[List[str]]:
    """
    Check if user can update/delete target account.

    Returns:
        Resolved allowed merchant_ids (None = unrestricted, [] = none).
        Callers can reuse this to avoid duplicate resolve_merchant_ids calls.

    Rules:
    - Admin: Can modify any account except other admins (unless self)
    - Reseller: Can modify user/merchant accounts in their scope
    - Merchant: Can modify user accounts for their merchant_id(s)
    - User: Cannot modify accounts
    """
    if current_user.role == UserRole.ADMIN:
        # Admin cannot modify other admin accounts (only self)
        if target_user.role == UserRole.ADMIN and target_user.id != current_user.id:
            raise HTTPException(
                status_code=403, detail="Cannot modify other admin accounts"
            )
        return None  # Admin unrestricted

    if current_user.role == UserRole.RESELLER:
        # Reseller cannot modify admin/reseller accounts
        if target_user.role in {UserRole.ADMIN, UserRole.RESELLER}:
            raise HTTPException(
                status_code=403, detail="Cannot modify admin/reseller accounts"
            )

        # Check merchant_ids scope (resolves wildcard through owner chain)
        allowed = await resolve_merchant_ids(current_user)
        if allowed is not None:
            # Reseller must have access to ALL of target's merchant_identifiers (subset check)
            if not all(mid in allowed for mid in target_user.merchant_identifiers):
                raise HTTPException(
                    status_code=403, detail="Target user is outside your merchant scope"
                )
        return allowed

    if current_user.role == UserRole.MERCHANT:
        # Merchant can only modify user accounts
        if target_user.role != UserRole.USER:
            raise HTTPException(status_code=403, detail="Can only modify user accounts")

        # Check merchant_identifiers scope (resolves wildcard through owner chain)
        allowed = await resolve_merchant_ids(current_user)
        if allowed is not None:
            # Merchant must have access to ALL of target's merchant_identifiers (subset check)
            if not all(mid in allowed for mid in target_user.merchant_identifiers):
                raise HTTPException(
                    status_code=403, detail="Target user is outside your merchant scope"
                )
        return allowed

    # User role cannot modify accounts
    raise HTTPException(
        status_code=403, detail="Insufficient permissions to modify accounts"
    )


async def _get_rbac_merchant_filter(current_user: UserInfo) -> Optional[List[str]]:
    """
    Get merchant_ids filter based on user role for RBAC.
    Resolves wildcard through owner chain for proper scoping.

    Returns:
        None for unrestricted access (admin, reseller with wildcard)
        merchant_ids list for scoped access
    """
    if current_user.role == UserRole.ADMIN:
        return None  # Admin sees all

    if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
        return await resolve_merchant_ids(current_user)

    # User role shouldn't access these endpoints
    raise HTTPException(
        status_code=403, detail="Insufficient permissions to list users"
    )


async def create_user_handler(
    user_data: UserCreate, current_user: UserInfo
) -> UserResponse:
    """Create a new user account."""
    # Validate merchant_identifiers requirement for merchant/user roles
    if user_data.role in {UserRole.MERCHANT, UserRole.USER}:
        if not user_data.merchant_identifiers:
            raise HTTPException(
                status_code=400,
                detail="merchant_identifiers are required for merchant/user roles",
            )

    # For reseller role, validate reseller_ids is provided
    if user_data.role == UserRole.RESELLER:
        if not user_data.reseller_ids:
            raise HTTPException(
                status_code=400,
                detail="reseller_ids are required for reseller role",
            )

    # Check creation permissions (validates both reseller_ids and merchant_identifiers)
    await _check_create_access(
        current_user,
        user_data.role,
        user_data.reseller_ids,
        user_data.merchant_identifiers,
    )

    try:
        # Check if username exists
        exists = await user_accessors.check_username_exists(user_data.username)
        if exists:
            raise HTTPException(
                status_code=409,
                detail=f"Username '{user_data.username}' already exists",
            )

        # Set owner_id: all non-admin accounts track who created them
        owner_id = (
            None
            if current_user.role == UserRole.ADMIN and user_data.role == UserRole.ADMIN
            else current_user.id
        )

        user = await user_accessors.create_user(
            id=str(uuid.uuid4()),
            username=user_data.username,
            password=user_data.password,
            role=user_data.role,
            email=user_data.email,
            reseller_ids=user_data.reseller_ids,
            merchant_identifiers=user_data.merchant_identifiers,
            is_active=user_data.is_active,
            owner_id=owner_id,
        )

        if not user:
            raise HTTPException(status_code=500, detail="Failed to create user")

        return user

    except asyncpg.UniqueViolationError:
        raise HTTPException(status_code=409, detail="ID or username already exists")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating user: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_all_users_handler(
    page: int,
    limit: int,
    username_filter: Optional[str],
    role_filter: Optional[str],
    reseller_id_filter: Optional[str],
    merchant_identifier_filter: Optional[str],
    is_active_filter: Optional[bool],
    sort_by: str,
    sort_order: str,
    current_user: UserInfo,
) -> UserListResponse:
    """Get all user accounts with pagination and RBAC filtering."""
    # Get RBAC merchant filter
    allowed_merchant_ids = await _get_rbac_merchant_filter(current_user)

    # Determine which roles to exclude based on current user's role
    excluded_roles = None
    if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
        # Cannot see admin/reseller accounts
        if role_filter and role_filter in {
            UserRole.ADMIN.value,
            UserRole.RESELLER.value,
        }:
            raise HTTPException(
                status_code=403, detail="Cannot filter by admin or reseller role"
            )
        excluded_roles = [UserRole.ADMIN.value, UserRole.RESELLER.value]

    try:
        users, total = await user_accessors.get_all_users(
            page=page,
            limit=limit,
            username_filter=username_filter,
            role_filter=role_filter,
            reseller_id_filter=reseller_id_filter,
            merchant_identifier_filter=merchant_identifier_filter,
            is_active_filter=is_active_filter,
            allowed_merchant_ids=allowed_merchant_ids,
            excluded_roles=excluded_roles,
            sort_by=sort_by,
            sort_order=sort_order,
        )

        total_pages = (total + limit - 1) // limit if total > 0 else 0

        return UserListResponse(
            users=users, total=total, page=page, limit=limit, total_pages=total_pages
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching users: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def get_user_by_id_handler(user_id: str, current_user: UserInfo) -> UserResponse:
    """Get user account by UUID."""
    try:
        user = await user_accessors.get_user_by_id(user_id)

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # RBAC check
        if current_user.role != UserRole.ADMIN:
            # Allow viewing self
            if user.id == current_user.id:
                return user

            # Check access based on role and merchant_ids
            if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
                # Deny access to admin and reseller accounts
                if user.role in {UserRole.ADMIN, UserRole.RESELLER}:
                    raise HTTPException(
                        status_code=403, detail="Access denied to this user"
                    )

                # Resolve wildcard through owner chain for proper scoping
                allowed = await resolve_merchant_ids(current_user)
                if allowed is not None:
                    # Must have at least one overlapping merchant_id
                    if not any(mid in allowed for mid in user.merchant_identifiers):
                        raise HTTPException(
                            status_code=403, detail="Access denied to this user"
                        )
            else:
                # User role cannot view other users
                raise HTTPException(
                    status_code=403, detail="Access denied to this user"
                )

        return user

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def update_user_handler(
    user_id: str, user_data: UserUpdate, current_user: UserInfo
) -> UserResponse:
    """Update user account."""
    try:
        # Get existing user
        user = await user_accessors.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Check update permissions (returns resolved merchant_ids to avoid duplicate DB calls)
        allowed = await _check_update_delete_access(current_user, user)

        # Validate merchant_identifiers update for merchant/user roles
        if user_data.merchant_identifiers is not None:
            if (
                user.role in {UserRole.MERCHANT, UserRole.USER}
                and not user_data.merchant_identifiers
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove all merchant_identifiers from merchant/user accounts",
                )

            # Check scope for reseller/merchant using already-resolved merchant_ids
            if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
                validate_merchant_ids_subset(
                    user_data.merchant_identifiers,
                    allowed,
                    "Cannot assign merchant_identifiers outside your scope",
                )

        # Validate reseller_ids update
        if user_data.reseller_ids is not None:
            # Only admin can change reseller_ids
            if current_user.role != UserRole.ADMIN:
                raise HTTPException(
                    status_code=403,
                    detail="Only admins can update reseller_ids",
                )

        updated_user = await user_accessors.update_user(
            user_id=user_id,
            password=user_data.password,
            email=user_data.email,
            reseller_ids=user_data.reseller_ids,
            merchant_identifiers=user_data.merchant_identifiers,
            is_active=user_data.is_active,
        )

        if not updated_user:
            raise HTTPException(status_code=404, detail="User not found")

        return updated_user

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def delete_user_handler(
    user_id: str, current_user: UserInfo
) -> DeleteUserResponse:
    """Delete user account."""
    # Admin accounts cannot be deleted by anyone (including self)
    if current_user.role == UserRole.ADMIN and user_id == current_user.id:
        raise HTTPException(status_code=403, detail="Admin accounts cannot be deleted")

    try:
        # Get existing user
        user = await user_accessors.get_user_by_id(user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # No one can delete an admin account
        if user.role == UserRole.ADMIN:
            raise HTTPException(
                status_code=403, detail="Admin accounts cannot be deleted"
            )

        # Cannot delete self
        if user.id == current_user.id:
            raise HTTPException(
                status_code=400, detail="Cannot delete your own account"
            )

        # Check delete permissions
        await _check_update_delete_access(current_user, user)
        success = await user_accessors.delete_user(user_id)

        if not success:
            raise HTTPException(status_code=404, detail="User not found")

        return DeleteUserResponse(
            success=True,
            message=f"User {user_id} deleted successfully",
            deleted_id=user_id,
        )

    except ValueError as e:
        # Admin protection (e.g., "Admin accounts cannot be deleted")
        raise HTTPException(status_code=403, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
