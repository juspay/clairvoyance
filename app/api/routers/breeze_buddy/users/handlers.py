"""
Handlers for user account management endpoints.
Implements business logic and RBAC for user login accounts.

RBAC rules for account creation:
- Admin: Can create reseller, merchant, user with any merchant_ids
- Reseller: Can create merchant/user with subset of own merchant_ids; becomes owner_id
- Merchant: Can create user with only own merchant_id(s); becomes owner_id
- User: Cannot create accounts
"""

from typing import List, Optional

import asyncpg
from fastapi import HTTPException

from app.core.logger import logger
from app.core.security.scope import (
    resolve_merchant_ids,
    validate_merchant_ids_subset,
)
from app.database.accessor.breeze_buddy import users as user_accessors
from app.database.accessor.breeze_buddy.merchants import (
    check_merchant_identifier_exists,
    get_merchants_by_reseller,
)
from app.database.accessor.breeze_buddy.resellers import (
    get_reseller_by_id,
    get_user_umbrella_grants,
    get_user_workspace_access,
)
from app.schemas import UserInfo, UserRole
from app.schemas.breeze_buddy.resellers import UserAccessResponse
from app.schemas.breeze_buddy.users import (
    DeleteUserResponse,
    UserCreate,
    UserListResponse,
    UserResponse,
    UserUpdate,
)


async def _get_reseller_merchants(reseller_id: str) -> List[str]:
    """Get all merchant_ids for a given reseller_id.

    Args:
        reseller_id: The reseller ID to look up

    Returns:
        List of merchant_ids for that reseller
    """

    merchants, _ = await get_merchants_by_reseller(reseller_id)
    return [m.merchant_id for m in merchants]


async def _check_create_access(
    current_user: UserInfo,
    target_role: UserRole,
    reseller_ids: List[str],
    merchant_ids: List[str],
):
    """
    Check if user can create accounts with target role and access identifiers.

    Validation flow:
    1. First validate reseller_ids (user must have access to those resellers).
    2. Then validate merchant_ids against what those resellers have.
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

        # Step 2: Validate merchant_ids
        # If wildcard ["*"], grant access to all merchants of the specified resellers
        # Otherwise, validate specific merchants are allowed
        if merchant_ids and merchant_ids != ["*"]:
            # Get all merchants under the specified resellers
            allowed_merchants = []
            for rid in reseller_ids:
                reseller_merchants = await _get_reseller_merchants(rid)
                allowed_merchants.extend(reseller_merchants)

            # Validate all requested merchants are in the allowed list
            invalid_merchants = [m for m in merchant_ids if m not in allowed_merchants]
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

        # Validate merchant_ids are within merchant's scope
        # If merchant has wildcard merchant_ids, they can assign wildcard to users
        if "*" in current_user.merchant_ids:
            # Merchant with wildcard can assign specific merchants or wildcard
            return

        allowed = await resolve_merchant_ids(current_user)
        validate_merchant_ids_subset(
            merchant_ids,
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
            # Resolve target's effective merchant_ids (handles wildcard via owner chain)
            # to prevent cross-tenant access when target has ["*"] scope.
            if "*" in (target_user.merchant_ids or []):
                target_info = UserInfo(
                    id=target_user.id,
                    username=target_user.username,
                    role=target_user.role,
                    reseller_ids=target_user.reseller_ids,
                    merchant_ids=target_user.merchant_ids,
                    owner_id=target_user.owner_id,
                )
                effective = await resolve_merchant_ids(target_info)
                # effective=None means admin-owned scope — deny for non-admin callers
                if effective is None or not all(mid in allowed for mid in effective):
                    raise HTTPException(
                        status_code=403,
                        detail="Target user is outside your merchant scope",
                    )
            elif not all(mid in allowed for mid in target_user.merchant_ids):
                raise HTTPException(
                    status_code=403, detail="Target user is outside your merchant scope"
                )
        return allowed

    if current_user.role == UserRole.MERCHANT:
        # Merchant can only modify user accounts
        if target_user.role != UserRole.USER:
            raise HTTPException(status_code=403, detail="Can only modify user accounts")

        # Check merchant_ids scope (resolves wildcard through owner chain)
        allowed = await resolve_merchant_ids(current_user)
        if allowed is not None:
            # Resolve target's effective merchant_ids (handles wildcard via owner chain)
            # to prevent cross-tenant access when target has ["*"] scope.
            if "*" in (target_user.merchant_ids or []):
                target_info = UserInfo(
                    id=target_user.id,
                    username=target_user.username,
                    role=target_user.role,
                    reseller_ids=target_user.reseller_ids,
                    merchant_ids=target_user.merchant_ids,
                    owner_id=target_user.owner_id,
                )
                effective = await resolve_merchant_ids(target_info)
                # effective=None means admin-owned scope — deny for non-admin callers
                if effective is None or not all(mid in allowed for mid in effective):
                    raise HTTPException(
                        status_code=403,
                        detail="Target user is outside your merchant scope",
                    )
            elif not all(mid in allowed for mid in target_user.merchant_ids):
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
        None for unrestricted access (admin only)
        merchant_ids list for scoped access (reseller/merchant/user)
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
    # Validate merchant_ids requirement for merchant/user roles
    if user_data.role in {UserRole.MERCHANT, UserRole.USER}:
        if not user_data.merchant_ids:
            raise HTTPException(
                status_code=400,
                detail="merchant_ids are required for merchant/user roles",
            )

    # For reseller role: merchant_ids is always ["*"] (resolved at
    # query time to merchants they own), and reseller_ids is always [own id].
    # These are not configurable — a reseller's scope is defined by ownership.
    if user_data.role == UserRole.RESELLER:
        user_data.merchant_ids = ["*"]
        user_data.reseller_ids = [user_data.id]

    # Check creation permissions (validates both reseller_ids and merchant_ids)
    await _check_create_access(
        current_user,
        user_data.role,
        user_data.reseller_ids,
        user_data.merchant_ids,
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
            id=user_data.id,
            username=user_data.username,
            password=user_data.password,
            role=user_data.role,
            email=user_data.email,
            reseller_ids=user_data.reseller_ids,
            merchant_ids=user_data.merchant_ids,
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
    owner_id_filter: Optional[str],
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
            owner_id_filter=owner_id_filter,
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
                    # Resolve target's effective merchant_ids (handles wildcard via owner
                    # chain) to prevent cross-tenant exposure when target has ["*"] scope.
                    if "*" in (user.merchant_ids or []):
                        target_info = UserInfo(
                            id=user.id,
                            username=user.username,
                            role=user.role,
                            reseller_ids=user.reseller_ids,
                            merchant_ids=user.merchant_ids,
                            owner_id=user.owner_id,
                        )
                        effective = await resolve_merchant_ids(target_info)
                        # effective=None means admin-owned scope — deny for non-admin callers
                        if effective is None or not any(
                            mid in allowed for mid in effective
                        ):
                            raise HTTPException(
                                status_code=403, detail="Access denied to this user"
                            )
                    elif not any(mid in allowed for mid in user.merchant_ids):
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


async def get_user_access_handler(
    user_id: str, current_user: UserInfo
) -> UserAccessResponse:
    """Effective access for one user, read from the normalized grant tables.

    Explicit workspace membership and umbrella grants are reported as-is;
    workspaces reachable through an all-workspaces umbrella grant appear with
    source='inherited'. Visibility RBAC is exactly GET /user/{id}'s.
    """
    # Reuse the single-user RBAC gate (raises 403/404 as appropriate).
    user = await get_user_by_id_handler(user_id, current_user)

    if user.role == UserRole.ADMIN:
        return UserAccessResponse(
            user_id=user.id, role=user.role.value, unrestricted=True
        )

    try:
        grants = await get_user_umbrella_grants(user.id)
        workspaces = await get_user_workspace_access(user.id)
        return UserAccessResponse(
            user_id=user.id,
            role=user.role.value,
            unrestricted=False,
            umbrella_grants=grants,
            workspaces=workspaces,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching access for user {user_id}: {e}")
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

        # Validate merchant_ids update for merchant/user roles
        if user_data.merchant_ids is not None:
            if (
                user.role in {UserRole.MERCHANT, UserRole.USER}
                and not user_data.merchant_ids
            ):
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove all merchant_ids from merchant/user accounts",
                )

            # Check scope for reseller/merchant using already-resolved merchant_ids
            if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
                validate_merchant_ids_subset(
                    user_data.merchant_ids,
                    allowed,
                    "Cannot assign merchant_ids outside your scope",
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
            merchant_ids=user_data.merchant_ids,
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


# ─────────────────────────────────────────────────────────────────────────────
# Membership management (first-class grant/revoke over the dual-written model)
#
# These endpoints stay array-authoritative: they compute the new
# reseller_ids/merchant_ids and write through update_user, whose dual-write
# keeps the grant tables in sync. They all return the refreshed effective
# access so the console can re-render without a second call.
# ─────────────────────────────────────────────────────────────────────────────


async def _load_target_for_access_change(
    user_id: str, current_user: UserInfo
) -> tuple[UserResponse, Optional[List[str]]]:
    """Fetch the target user and run the update RBAC gate."""
    user = await user_accessors.get_user_by_id(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == UserRole.ADMIN:
        raise HTTPException(
            status_code=400,
            detail="Admin accounts have unrestricted access and take no grants",
        )
    allowed = await _check_update_delete_access(current_user, user)
    return user, allowed


async def add_user_workspace_handler(
    user_id: str, merchant_id: str, current_user: UserInfo
) -> UserAccessResponse:
    """Grant a user explicit membership of one workspace."""
    try:
        user, allowed = await _load_target_for_access_change(user_id, current_user)

        if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
            validate_merchant_ids_subset(
                [merchant_id],
                allowed,
                "Cannot grant workspaces outside your scope",
            )

        if not await check_merchant_identifier_exists(merchant_id):
            raise HTTPException(status_code=404, detail="Workspace not found")

        current = user.merchant_ids or []
        new_list = current if merchant_id in current else current + [merchant_id]
        # Always write, even when the array already lists the workspace: the
        # dual-write re-projection is idempotent, and this heals the one drift
        # case where the array gained the id before the workspace existed (the
        # projection skipped it then; now that it exists, the row lands).
        await user_accessors.update_user(user_id=user_id, merchant_ids=new_list)

        return await get_user_access_handler(user_id, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error granting workspace {merchant_id} to {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def remove_user_workspace_handler(
    user_id: str, merchant_id: str, current_user: UserInfo
) -> UserAccessResponse:
    """Revoke a user's explicit membership of one workspace."""
    try:
        user, allowed = await _load_target_for_access_change(user_id, current_user)

        if current_user.role in {UserRole.RESELLER, UserRole.MERCHANT}:
            validate_merchant_ids_subset(
                [merchant_id],
                allowed,
                "Cannot revoke workspaces outside your scope",
            )

        current = user.merchant_ids or []
        if merchant_id not in current:
            raise HTTPException(
                status_code=404, detail="User has no explicit membership here"
            )

        remaining = [m for m in current if m != merchant_id]
        if user.role in {UserRole.MERCHANT, UserRole.USER} and not remaining:
            raise HTTPException(
                status_code=400,
                detail="Cannot remove the last workspace from merchant/user accounts",
            )

        await user_accessors.update_user(user_id=user_id, merchant_ids=remaining)
        return await get_user_access_handler(user_id, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking workspace {merchant_id} from {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def set_user_umbrella_handler(
    user_id: str, reseller_id: str, all_workspaces: bool, current_user: UserInfo
) -> UserAccessResponse:
    """Grant (or update) a user's umbrella affiliation. Admin only.

    all_workspaces=true maps to the legacy merchant_ids=["*"] wildcard, which
    is per-account, not per-umbrella — so it is refused while the user holds
    any other umbrella affiliation.
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Only admins can manage umbrella grants"
        )

    try:
        user, _ = await _load_target_for_access_change(user_id, current_user)

        if not await get_reseller_by_id(reseller_id):
            raise HTTPException(status_code=404, detail="Reseller not found")

        current_r = user.reseller_ids or []
        current_m = user.merchant_ids or []

        if all_workspaces and any(r != reseller_id for r in current_r):
            raise HTTPException(
                status_code=409,
                detail="all_workspaces is account-wide in the legacy arrays; "
                "remove the user's other umbrella affiliations first",
            )

        new_r = current_r if reseller_id in current_r else current_r + [reseller_id]
        if all_workspaces:
            new_m = current_m if "*" in current_m else current_m + ["*"]
        else:
            if "*" in current_m and any(r != reseller_id for r in current_r):
                # Mirror of the guard above: the legacy wildcard is
                # account-wide, so stripping it here would silently downgrade
                # ANOTHER umbrella's all-workspaces grant as a side effect.
                raise HTTPException(
                    status_code=409,
                    detail="This account holds the account-wide all-workspaces "
                    "wildcard for another umbrella; change that grant first",
                )
            new_m = [m for m in current_m if m != "*"]
            if user.role in {UserRole.MERCHANT, UserRole.USER} and not new_m:
                raise HTTPException(
                    status_code=400,
                    detail="Removing all-workspaces would leave this account "
                    "with no workspace; grant an explicit workspace first",
                )

        await user_accessors.update_user(
            user_id=user_id,
            reseller_ids=new_r,
            merchant_ids=new_m if new_m != current_m else None,
        )
        return await get_user_access_handler(user_id, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error setting umbrella {reseller_id} on {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


async def revoke_user_umbrella_handler(
    user_id: str, reseller_id: str, current_user: UserInfo
) -> UserAccessResponse:
    """Revoke a user's umbrella affiliation. Admin only."""
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=403, detail="Only admins can manage umbrella grants"
        )

    try:
        user, _ = await _load_target_for_access_change(user_id, current_user)

        if user.role == UserRole.RESELLER and reseller_id == user.id:
            raise HTTPException(
                status_code=400,
                detail="A reseller's own umbrella is intrinsic; deactivate or "
                "delete the login instead",
            )

        current_r = user.reseller_ids or []
        if reseller_id not in current_r:
            raise HTTPException(
                status_code=404, detail="User has no affiliation with this umbrella"
            )

        new_r = [r for r in current_r if r != reseller_id]
        current_m = user.merchant_ids or []
        new_m = current_m
        if not new_r and "*" in current_m:
            # An umbrella-less wildcard would read as admin-style unrestricted;
            # never leave one behind.
            new_m = [m for m in current_m if m != "*"]
            if user.role in {UserRole.MERCHANT, UserRole.USER} and not new_m:
                raise HTTPException(
                    status_code=400,
                    detail="Revoking this umbrella would leave the account "
                    "with no workspace; grant an explicit workspace first",
                )

        await user_accessors.update_user(
            user_id=user_id,
            reseller_ids=new_r,
            merchant_ids=new_m if new_m != current_m else None,
        )
        return await get_user_access_handler(user_id, current_user)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error revoking umbrella {reseller_id} from {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
