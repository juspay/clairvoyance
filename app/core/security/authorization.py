"""
Generic RBAC authorization utilities.
Provides role and permission checking that can be used across all agents.

Role hierarchy: admin > reseller > merchant > user
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_reseller
from app.database.accessor.breeze_buddy.users import (
    get_user_by_id,
)
from app.schemas import UserInfo, UserRole


async def _get_merchant_identifiers_by_reseller(reseller_id: str) -> List[str]:
    """Fetch all merchant_identifiers under a reseller from the database."""

    merchants, _ = await get_merchants_by_reseller(reseller_id, page=1, limit=1000)
    return [m.merchant_identifier for m in merchants]


def check_permission(current_user: UserInfo, required_permission: str) -> bool:
    """
    Check if user has a specific permission.

    Args:
        current_user: Current authenticated user
        required_permission: Permission to check (e.g., "read:all", "write:own_data")

    Returns:
        True if user has the permission, False otherwise
    """
    # Admins have all permissions
    if current_user.role == UserRole.ADMIN:
        return True

    # Check if user has the specific permission
    return required_permission in current_user.permissions


def require_permission(current_user: UserInfo, required_permission: str) -> None:
    """
    Require user to have a specific permission.

    Args:
        current_user: Current authenticated user
        required_permission: Permission required

    Raises:
        HTTPException: 403 Forbidden if user doesn't have permission
    """
    if not check_permission(current_user, required_permission):
        logger.warning(
            f"User {current_user.username} lacks permission: {required_permission}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient permissions. Required: {required_permission}",
        )


def require_role(current_user: UserInfo, required_roles: List[UserRole]) -> None:
    """
    Require user to have one of the specified roles.

    Args:
        current_user: Current authenticated user
        required_roles: List of acceptable roles

    Raises:
        HTTPException: 403 Forbidden if user doesn't have required role
    """
    if current_user.role not in required_roles:
        logger.warning(
            f"User {current_user.username} (role: {current_user.role}) "
            f"attempted action requiring roles: {required_roles}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Insufficient role. Required one of: {[r.value for r in required_roles]}",
        )


def require_admin(current_user: UserInfo) -> None:
    """
    Require user to be an admin.

    Args:
        current_user: Current authenticated user

    Raises:
        HTTPException: 403 Forbidden if user is not admin
    """
    if current_user.role != UserRole.ADMIN:
        logger.warning(
            f"Non-admin user {current_user.username} attempted admin-only action"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )


def is_admin(current_user: UserInfo) -> bool:
    """
    Check if user is an admin.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is admin, False otherwise
    """
    return current_user.role == UserRole.ADMIN


def is_reseller(current_user: UserInfo) -> bool:
    """
    Check if user is a reseller.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is reseller, False otherwise
    """
    return current_user.role == UserRole.RESELLER


def is_merchant(current_user: UserInfo) -> bool:
    """
    Check if user is a merchant.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is merchant, False otherwise
    """
    return current_user.role == UserRole.MERCHANT


def is_user(current_user: UserInfo) -> bool:
    """
    Check if user is a user-level (shop) account.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user is a regular user, False otherwise
    """
    return current_user.role == UserRole.USER


async def _resolve_merchants_for_reseller_ids(
    reseller_ids: List[str],
) -> List[str]:
    """Fetch all merchant_identifiers across multiple reseller IDs.

    Queries the merchants table for each reseller_id and returns
    the combined list of merchant_identifiers.

    Args:
        reseller_ids: List of reseller IDs to look up

    Returns:
        Combined deduplicated list of merchant_identifiers from all given resellers
    """
    all_merchant_ids: set[str] = set()
    for reseller_id in reseller_ids:
        merchant_ids_for_reseller = await _get_merchant_identifiers_by_reseller(
            reseller_id
        )
        all_merchant_ids.update(merchant_ids_for_reseller)
    return list(all_merchant_ids)


async def _resolve_user_scopes(
    reseller_ids: List[str],
    merchant_identifiers: List[str],
    owner_id: Optional[str],
    username: str,
    depth: int = 0,
) -> Optional[List[str]]:
    """Resolve effective merchant_identifiers by walking the owner chain.

    Tries to resolve from the user's own scopes first. If wildcards are present,
    walks up the owner chain to inherit the creator's scope.

    Important: reseller_ids in the users table are grouping labels (e.g., "BB_SHOPIFY"),
    NOT user UUIDs. The merchants table's reseller_id column stores the owner's user UUID.
    So to resolve merchant_identifiers=["*"], we walk the owner chain and use the
    reseller/admin user's UUID to query the merchants table.

    Args:
        reseller_ids: User's reseller_ids (grouping labels, not UUIDs)
        merchant_identifiers: User's merchant_identifiers
        owner_id: User's owner_id (who created them - this IS a UUID)
        username: For logging
        depth: Current chain depth (max 3 to prevent infinite loops)

    Returns:
        None only if an admin is reached (truly unrestricted)
        List of specific merchant_identifiers otherwise
    """
    if depth > 3:
        logger.warning(
            f"Owner chain depth exceeded for user {username}, returning empty scope"
        )
        return []

    has_merchant_wildcard = "*" in merchant_identifiers

    # Case: No wildcard in merchants → return as-is
    if not has_merchant_wildcard:
        return merchant_identifiers if merchant_identifiers else []

    # Case: Wildcard merchants → walk up the owner chain to find scope
    # We use owner_id (UUID) to query merchants table, not reseller_ids (labels)
    if not owner_id:
        logger.warning(
            f"User {username} has wildcard scope but no owner_id to resolve from"
        )
        return []

    try:
        owner = await get_user_by_id(owner_id)
    except Exception as e:
        logger.error(f"Failed to resolve owner {owner_id} for user {username}: {e}")
        return []

    if not owner:
        logger.warning(f"User {username} has owner_id {owner_id} but owner not found")
        return []

    # Owner is admin → truly unrestricted
    if owner.role == UserRole.ADMIN:
        return None

    # Owner is reseller → use owner's UUID to query merchants they own
    if owner.role == UserRole.RESELLER:
        if "*" not in (owner.merchant_identifiers or []):
            # Owner has specific merchant_identifiers → use them
            return owner.merchant_identifiers or []
        # Owner also has wildcard → query merchants owned by this reseller (by UUID)
        merchants = await _get_merchant_identifiers_by_reseller(owner.id)
        return merchants if merchants else []

    # Owner is merchant → use owner's merchant_identifiers if specific
    if "*" not in (owner.merchant_identifiers or []):
        return owner.merchant_identifiers or []

    # Owner also has wildcard → recurse up the chain
    return await _resolve_user_scopes(
        reseller_ids=owner.reseller_ids or [],
        merchant_identifiers=owner.merchant_identifiers or [],
        owner_id=owner.owner_id,
        username=f"{username}->owner:{owner.username}",
        depth=depth + 1,
    )


async def resolve_merchant_ids(user: UserInfo) -> Optional[List[str]]:
    """Resolve effective merchant_identifiers for a user, handling wildcard ["*"].

    Returns:
        None if user has unrestricted access (admin is in the owner chain)
        List of specific merchant_identifiers otherwise

    Resolution rules (hierarchical):
    1. Admin: always returns None (unrestricted)
    2. Specific merchant_identifiers (no wildcard): return as-is
    3. Specific reseller_ids + wildcard merchants: resolve from DB
    4. Both wildcards: walk owner chain until a non-wildcard scope or admin is found

    Owner chain examples:
    - User(*,*) → Merchant(R1,*) → resolves to all merchants of R1
    - User(*,*) → Merchant(*,*) → Reseller(R1,R2,*) → resolves to all merchants of R1+R2
    - User(*,*) → Merchant(*,*) → Reseller(*,*) → Admin → None (unrestricted)
    """
    if user.role == UserRole.ADMIN:
        return None  # Admin always unrestricted

    reseller_ids = user.reseller_ids or []
    merchant_ids = user.merchant_identifiers or []

    # If no wildcards in merchant_identifiers, return directly
    if "*" not in merchant_ids:
        return merchant_ids if merchant_ids else []

    # Delegate to the recursive resolver
    return await _resolve_user_scopes(
        reseller_ids=reseller_ids,
        merchant_identifiers=merchant_ids,
        owner_id=user.owner_id,
        username=user.username,
    )


def validate_merchant_ids_subset(
    requested_ids: List[str],
    allowed_ids: Optional[List[str]],
    error_message: str = "Cannot assign merchant_ids outside your scope",
) -> None:
    """Validate that requested merchant_ids are a subset of allowed IDs.

    Args:
        requested_ids: The merchant_ids being requested
        allowed_ids: The allowed merchant_ids (None = unrestricted)
        error_message: Error message for 403 response

    Raises:
        HTTPException: 403 if requested_ids are not a subset of allowed_ids
    """
    if allowed_ids is None:
        return  # Unrestricted access

    if not all(mid in allowed_ids for mid in requested_ids):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_message,
        )


async def _resolve_reseller_scopes(
    reseller_ids: List[str],
    owner_id: Optional[str],
    username: str,
    depth: int = 0,
) -> Optional[List[str]]:
    """Resolve effective reseller_ids by walking the owner chain.

    If reseller_ids contains ["*"], walks up the owner chain to inherit
    the creator's reseller_ids scope.

    Args:
        reseller_ids: User's reseller_ids
        owner_id: User's owner_id (who created them)
        username: For logging
        depth: Current chain depth (max 3 to prevent infinite loops)

    Returns:
        None only if an admin is reached (truly unrestricted)
        List of specific reseller_ids otherwise
    """
    if depth > 3:
        logger.warning(
            f"Owner chain depth exceeded for reseller_ids resolution of user {username}"
        )
        return []

    # If no wildcard, return as-is
    if "*" not in reseller_ids:
        return reseller_ids if reseller_ids else []

    # Wildcard → walk up the owner chain
    if not owner_id:
        logger.warning(
            f"User {username} has wildcard reseller_ids but no owner_id to resolve from"
        )
        return []

    try:
        owner = await get_user_by_id(owner_id)
    except Exception as e:
        logger.error(
            f"Failed to resolve owner {owner_id} for reseller_ids of user {username}: {e}"
        )
        return []

    if not owner:
        logger.warning(f"User {username} has owner_id {owner_id} but owner not found")
        return []

    # Owner is admin → truly unrestricted
    if owner.role == UserRole.ADMIN:
        return None

    # Recurse into owner's reseller_ids
    return await _resolve_reseller_scopes(
        reseller_ids=owner.reseller_ids or [],
        owner_id=owner.owner_id,
        username=f"{username}->owner:{owner.username}",
        depth=depth + 1,
    )


async def resolve_reseller_ids(user: UserInfo) -> Optional[List[str]]:
    """Resolve effective reseller_ids for a user, handling wildcard ["*"].

    Returns:
        None if user has unrestricted access (admin is in the owner chain)
        List of specific reseller_ids otherwise

    Resolution rules:
    1. Admin: always returns None (unrestricted)
    2. Specific reseller_ids (no wildcard): return as-is
    3. Wildcard ["*"]: walk owner chain until a non-wildcard scope or admin is found
    """
    if user.role == UserRole.ADMIN:
        return None

    reseller_ids = user.reseller_ids or []

    if "*" not in reseller_ids:
        return reseller_ids if reseller_ids else []

    return await _resolve_reseller_scopes(
        reseller_ids=reseller_ids,
        owner_id=user.owner_id,
        username=user.username,
    )
