"""
Breeze Buddy specific authorization logic for shop access control.
Implements multi-shop RBAC authorization based on reseller_ids and merchant_identifiers.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def get_accessible_resellers(reseller_ids: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible resellers, or None if access to ALL resellers.

    Args:
        reseller_ids: reseller_ids array from JWT token

    Returns:
        None if user has access to ALL resellers (["*"])
        List[str] of specific reseller_ids otherwise
    """
    if "*" in reseller_ids:
        return None  # None means "all resellers"
    else:
        return reseller_ids


def get_accessible_merchants(merchant_ids: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible merchants, or None if access to ALL merchants.

    Args:
        merchant_ids: merchant_ids array from JWT token

    Returns:
        None if user has access to ALL merchants (["*"])
        List[str] of specific merchant_ids otherwise
    """
    if "*" in merchant_ids:
        return None  # None means "all merchants"
    else:
        return merchant_ids


def validate_merchant_access(
    current_user: UserInfo,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = None,
) -> None:
    """
    Validate if current user has access to requested merchant(s).

    Args:
        current_user: Current authenticated user
        merchant_id: Single merchant identifier to check (optional)
        merchant_ids: Multiple merchant identifiers to check (optional)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_merchants = get_accessible_merchants(current_user.merchant_ids)

    # User has access to all merchants (admin/reseller with wildcard)
    if accessible_merchants is None:
        return  # Allow access

    # Check single merchant access
    if merchant_id:
        if merchant_id not in accessible_merchants:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized merchant: {merchant_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
            )

    # Check multiple merchants access
    if merchant_ids:
        unauthorized_merchants = [
            merchant
            for merchant in merchant_ids
            if merchant not in accessible_merchants
        ]

        if unauthorized_merchants:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized merchants: {unauthorized_merchants}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to one or more requested merchants: {unauthorized_merchants}",
            )


def apply_merchant_filter(
    current_user: UserInfo,
    requested_merchant_identifier: Optional[str] = None,
    requested_merchant_identifiers: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """
    Apply merchant filter based on user's accessible merchants.

    This function validates access and returns the appropriate merchant filter to use in queries.

    Args:
        current_user: Current authenticated user
        requested_merchant_identifier: Single merchant requested by user (optional)
        requested_merchant_identifiers: Multiple merchants requested by user (optional)

    Returns:
        None if user has access to ALL merchants (no filter needed)
        List[str] of merchant identifiers to filter by

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_merchants = get_accessible_merchants(current_user.merchant_ids)

    # User has access to all merchants (admin/reseller with wildcard)
    if accessible_merchants is None:
        # If user requested specific merchant(s), return that
        if requested_merchant_identifier:
            return [requested_merchant_identifier]
        if requested_merchant_identifiers:
            return requested_merchant_identifiers
        # Otherwise, no filter (return all merchants)
        return None

    # User has access to specific merchants only
    # Validate requested merchants
    if requested_merchant_identifier:
        if requested_merchant_identifier not in accessible_merchants:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized merchant: {requested_merchant_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {requested_merchant_identifier}",
            )
        return [requested_merchant_identifier]

    if requested_merchant_identifiers:
        unauthorized_merchants = [
            merchant
            for merchant in requested_merchant_identifiers
            if merchant not in accessible_merchants
        ]

        if unauthorized_merchants:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized merchants: {unauthorized_merchants}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchants: {unauthorized_merchants}",
            )
        return requested_merchant_identifiers

    # No specific merchants requested, return user's accessible merchants
    return accessible_merchants


def filter_by_merchant_access(
    current_user: UserInfo,
    data_list: List[dict],
    merchant_key: str = "merchant_id",
) -> List[dict]:
    """
    Filter a list of data by user's accessible merchants.

    Args:
        current_user: Current authenticated user
        data_list: List of dictionaries containing data
        merchant_key: Key name for merchant identifier in each dict (default: "merchant_id")

    Returns:
        Filtered list containing only accessible merchants
    """
    identifier = current_user.merchant_ids
    accessible_merchants = get_accessible_merchants(identifier)

    # User has access to all merchants
    if accessible_merchants is None:
        return data_list

    # Filter by accessible merchants
    return [
        item
        for item in data_list
        if merchant_key in item and item[merchant_key] in accessible_merchants
    ]


def has_wildcard_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all merchants) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard access, False otherwise
    """
    return "*" in current_user.merchant_ids


def has_wildcard_reseller_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all resellers) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard reseller access, False otherwise
    """
    return "*" in current_user.reseller_ids


def validate_reseller_access(
    current_user: UserInfo,
    reseller_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
) -> None:
    """
    Validate if current user has access to requested reseller(s).

    Args:
        current_user: Current authenticated user
        reseller_id: Single reseller ID to check (optional)
        reseller_ids: Multiple reseller IDs to check (optional)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_resellers = get_accessible_resellers(current_user.reseller_ids)

    # User has access to all resellers (admin/reseller with wildcard)
    if accessible_resellers is None:
        return  # Allow access

    # Check single reseller access
    if reseller_id:
        if reseller_id not in accessible_resellers:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized reseller: {reseller_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to reseller {reseller_id}",
            )

    # Check multiple resellers access
    if reseller_ids:
        unauthorized_resellers = [
            r for r in reseller_ids if r not in accessible_resellers
        ]

        if unauthorized_resellers:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized resellers: {unauthorized_resellers}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to one or more requested resellers: {unauthorized_resellers}",
            )


def apply_reseller_merchant_filter(
    current_user: UserInfo,
    requested_reseller_id: Optional[str] = None,
    requested_merchant_identifier: Optional[str] = None,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Apply hierarchical merchant and shop filter based on user's access.

    This function validates access and returns appropriate filters for queries.
    Handles the hierarchy: reseller_ids -> merchant_ids

    Args:
        current_user: Current authenticated user
        requested_reseller_id: Specific reseller requested (optional)
        requested_merchant_id: Specific shop requested (optional)

    Returns:
        Tuple of (reseller_ids_filter, merchant_ids_filter)
        None in either position means no filter needed (access to all)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_resellers = get_accessible_resellers(current_user.reseller_ids)
    accessible_merchants = get_accessible_merchants(current_user.merchant_ids)

    # Determine reseller filter
    reseller_filter = None
    if accessible_resellers is not None:
        # User has specific reseller access
        if requested_reseller_id:
            if requested_reseller_id not in accessible_resellers:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized reseller: {requested_reseller_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {requested_reseller_id}",
                )
            reseller_filter = [requested_reseller_id]
        else:
            reseller_filter = accessible_resellers
    else:
        # User has wildcard reseller access
        if requested_reseller_id:
            reseller_filter = [requested_reseller_id]
        # else: No filter needed (all resellers)

    # Determine merchant filter
    merchant_filter = None
    if accessible_merchants is not None:
        # User has specific merchant access
        if requested_merchant_identifier:
            if requested_merchant_identifier not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {requested_merchant_identifier}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {requested_merchant_identifier}",
                )
            merchant_filter = [requested_merchant_identifier]
        else:
            merchant_filter = accessible_merchants
    else:
        # User has wildcard merchant access
        if requested_merchant_identifier:
            merchant_filter = [requested_merchant_identifier]
        # else: No filter needed (all merchants)

    return (reseller_filter, merchant_filter)
