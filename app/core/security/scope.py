"""
Scope resolution utilities for RBAC wildcard handling.

Resolves wildcard ["*"] in reseller_ids and merchant_ids
by walking the owner chain to determine effective access scope.

Role hierarchy: admin > reseller > merchant > user
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.merchants import get_merchants_by_reseller
from app.database.accessor.breeze_buddy.users import get_user_by_id
from app.schemas import UserInfo, UserRole


async def _get_merchant_identifiers_by_reseller(reseller_id: str) -> List[str]:
    """Fetch all merchant_ids under a reseller from the database."""

    merchants, _ = await get_merchants_by_reseller(reseller_id, page=1, limit=1000)
    return [m.merchant_id for m in merchants]


async def _resolve_user_scopes(
    reseller_ids: List[str],
    merchant_ids: List[str],
    owner_id: Optional[str],
    username: str,
    depth: int = 0,
) -> Optional[List[str]]:
    """Resolve effective merchant_ids by walking the owner chain.

    Tries to resolve from the user's own scopes first. If wildcards are present,
    walks up the owner chain to inherit the creator's scope.

    Args:
        reseller_ids: User's reseller_ids (references users.id of reseller accounts)
        merchant_ids: User's merchant_ids
        owner_id: User's owner_id (who created them — references users.id)
        username: For logging
        depth: Current chain depth (max 3 to prevent infinite loops)

    Returns:
        None only if an admin is reached (truly unrestricted)
        List of specific merchant_ids otherwise
    """
    if depth > 3:
        logger.warning(
            f"Owner chain depth exceeded for user {username}, returning empty scope"
        )
        return []

    has_merchant_wildcard = "*" in merchant_ids

    # Case: No wildcard in merchants → return as-is
    if not has_merchant_wildcard:
        return merchant_ids if merchant_ids else []

    # Case: Wildcard merchants → walk up the owner chain to find scope.
    # A broken owner chain must FAIL CLOSED (return []) — only a positively
    # confirmed admin owner may yield unrestricted (None). Returning None here
    # let a wildcard token whose owner is missing/unresolvable mint platform-wide
    # merchant access (PT-20).
    if not owner_id:
        logger.warning(
            f"User {username} has wildcard merchant_ids with no owner_id — "
            "denying (failing closed)"
        )
        return []

    try:
        owner = await get_user_by_id(owner_id)
    except Exception as e:
        logger.error(
            f"Failed to resolve owner {owner_id} for user {username}: {e} — "
            "denying (failing closed)"
        )
        return []  # Can't resolve → deny rather than grant unrestricted

    if not owner:
        logger.warning(
            f"User {username} has owner_id {owner_id} but owner not found in DB — "
            "denying merchant access (failing closed)"
        )
        return []

    # Owner is admin → truly unrestricted
    if owner.role == UserRole.ADMIN:
        return None

    # Owner is reseller → query merchants they own (by their users.id)
    if owner.role == UserRole.RESELLER:
        if "*" not in (owner.merchant_ids or []):
            # Owner has specific merchant_ids → use them
            return owner.merchant_ids or []
        # Owner also has wildcard → query merchants owned by this reseller
        merchants = await _get_merchant_identifiers_by_reseller(owner.id)
        return merchants if merchants else []

    # Owner is merchant → use owner's merchant_ids if specific
    if "*" not in (owner.merchant_ids or []):
        return owner.merchant_ids or []

    # Owner also has wildcard → recurse up the chain
    return await _resolve_user_scopes(
        reseller_ids=owner.reseller_ids or [],
        merchant_ids=owner.merchant_ids or [],
        owner_id=owner.owner_id,
        username=f"{username}->owner:{owner.username}",
        depth=depth + 1,
    )


async def resolve_merchant_ids(user: UserInfo) -> Optional[List[str]]:
    """Resolve effective merchant_ids for a user, handling wildcard ["*"].

    Returns:
        None if user has unrestricted access (admin is in the owner chain)
        List of specific merchant_ids otherwise

    Resolution rules (hierarchical):
    1. Admin: always returns None (unrestricted)
    2. Specific merchant_ids (no wildcard): return as-is
    3. Specific reseller_ids + wildcard merchants: resolve from DB
    4. Both wildcards: walk owner chain until a non-wildcard scope or admin is found

    Owner chain examples:
    - Reseller(*) → resolves to merchants they own (reseller_id = their users.id)
    - User(*,*) → Merchant(R1,*) → resolves to all merchants of R1
    - User(*,*) → Merchant(*,*) → Reseller → resolves to merchants owned by that reseller
    - User(*,*) → Admin owner → None (unrestricted, admin-created accounts only)
    """
    if user.role == UserRole.ADMIN:
        return None  # Admin always unrestricted

    reseller_ids = user.reseller_ids or []
    merchant_ids = user.merchant_ids or []

    # If no wildcards in merchant_ids, return directly
    if "*" not in merchant_ids:
        return merchant_ids if merchant_ids else []

    # Reseller with wildcard → resolve to merchants they OWN.
    # Never return None for resellers — they should only see their own merchants,
    # not every merchant in the system.
    if user.role == UserRole.RESELLER:
        merchants = await _get_merchant_identifiers_by_reseller(user.id)
        return merchants if merchants else []

    # Merchant/User with wildcard → walk owner chain
    return await _resolve_user_scopes(
        reseller_ids=reseller_ids,
        merchant_ids=merchant_ids,
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

    # Wildcard → walk up the owner chain. Fail CLOSED on a broken chain — only a
    # confirmed admin owner yields unrestricted (None). Returning None on a
    # missing/unresolvable owner minted platform-wide reseller access (PT-20).
    if not owner_id:
        logger.warning(
            f"User {username} has wildcard reseller_ids with no owner_id — "
            "denying (failing closed)"
        )
        return []

    try:
        owner = await get_user_by_id(owner_id)
    except Exception as e:
        logger.error(
            f"Failed to resolve owner {owner_id} for reseller_ids of user "
            f"{username}: {e} — denying (failing closed)"
        )
        return []  # Can't resolve → deny rather than grant unrestricted

    if not owner:
        logger.warning(
            f"User {username} has owner_id {owner_id} but owner not found in DB — "
            "denying reseller access (failing closed)"
        )
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

    # Reseller's reseller_ids is always just [their own id].
    # A reseller IS a reseller — they don't belong to other resellers.
    if user.role == UserRole.RESELLER:
        return [user.id]

    reseller_ids = user.reseller_ids or []

    if "*" not in reseller_ids:
        return reseller_ids if reseller_ids else []

    return await _resolve_reseller_scopes(
        reseller_ids=reseller_ids,
        owner_id=user.owner_id,
        username=user.username,
    )
