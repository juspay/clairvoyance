"""
Breeze Buddy specific authorization logic for shop access control.
Implements multi-shop RBAC authorization based on reseller_ids and merchant_identifiers.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def get_accessible_merchants(reseller_ids: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible merchants, or None if access to ALL merchants.

    Args:
        reseller_ids: reseller_ids array from JWT token

    Returns:
        None if user has access to ALL merchants (["*"])
        List[str] of specific reseller_ids otherwise
    """
    if "*" in reseller_ids:
        return None  # None means "all merchants"
    else:
        return reseller_ids


def get_accessible_shops(merchant_identifiers: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible shops, or None if access to ALL shops.

    Args:
        merchant_identifiers: merchant_identifiers array from JWT token

    Returns:
        None if user has access to ALL shops (["*"])
        List[str] of specific merchant_identifiers otherwise
    """
    if "*" in merchant_identifiers:
        return None  # None means "all shops"
    else:
        return merchant_identifiers


def validate_shop_access(
    current_user: UserInfo,
    merchant_identifier: Optional[str] = None,
    merchant_identifiers: Optional[List[str]] = None,
) -> None:
    """
    Validate if current user has access to requested shop(s).

    Args:
        current_user: Current authenticated user
        merchant_identifier: Single shop identifier to check (optional)
        merchant_identifiers: Multiple shop identifiers to check (optional)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_shops = get_accessible_shops(current_user.merchant_identifiers)

    # User has access to all shops (admin/reseller with wildcard)
    if accessible_shops is None:
        return  # Allow access

    # Check single shop access
    if merchant_identifier:
        if merchant_identifier not in accessible_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shop: {merchant_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {merchant_identifier}",
            )

    # Check multiple shops access
    if merchant_identifiers:
        unauthorized_shops = [
            shop for shop in merchant_identifiers if shop not in accessible_shops
        ]

        if unauthorized_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shops: {unauthorized_shops}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to one or more requested shops: {unauthorized_shops}",
            )


def apply_shop_filter(
    current_user: UserInfo,
    requested_merchant_identifier: Optional[str] = None,
    requested_merchant_identifiers: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """
    Apply shop filter based on user's accessible shops.

    This function validates access and returns the appropriate shop filter to use in queries.

    Args:
        current_user: Current authenticated user
        requested_merchant_identifier: Single merchant requested by user (optional)
        requested_merchant_identifiers: Multiple merchants requested by user (optional)

    Returns:
        None if user has access to ALL shops (no filter needed)
        List[str] of shop identifiers to filter by

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_shops = get_accessible_shops(current_user.merchant_identifiers)

    # User has access to all shops (admin/reseller with wildcard)
    if accessible_shops is None:
        # If user requested specific shop(s), return that
        if requested_merchant_identifier:
            return [requested_merchant_identifier]
        if requested_merchant_identifiers:
            return requested_merchant_identifiers
        # Otherwise, no filter (return all shops)
        return None

    # User has access to specific shops only
    # Validate requested shops
    if requested_merchant_identifier:
        if requested_merchant_identifier not in accessible_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shop: {requested_merchant_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {requested_merchant_identifier}",
            )
        return [requested_merchant_identifier]

    if requested_merchant_identifiers:
        unauthorized_shops = [
            shop
            for shop in requested_merchant_identifiers
            if shop not in accessible_shops
        ]

        if unauthorized_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shops: {unauthorized_shops}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shops: {unauthorized_shops}",
            )
        return requested_merchant_identifiers

    # No specific shops requested, return user's accessible shops
    return accessible_shops


def filter_by_shop_access(
    current_user: UserInfo,
    data_list: List[dict],
    merchant_key: str = "merchant_identifier",
) -> List[dict]:
    """
    Filter a list of data by user's accessible merchants.

    Args:
        current_user: Current authenticated user
        data_list: List of dictionaries containing data
        merchant_key: Key name for shop identifier in each dict (default: "merchant_identifier")

    Returns:
        Filtered list containing only accessible shops
    """
    identifier = current_user.merchant_identifiers
    accessible_shops = get_accessible_shops(identifier)

    # User has access to all shops
    if accessible_shops is None:
        return data_list

    # Filter by accessible shops
    return [
        item
        for item in data_list
        if merchant_key in item and item[merchant_key] in accessible_shops
    ]


def has_wildcard_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all shops) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard access, False otherwise
    """
    return "*" in current_user.merchant_identifiers


def has_wildcard_merchant_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all merchants) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard reseller access, False otherwise
    """
    return "*" in current_user.reseller_ids


def validate_merchant_access(
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
    accessible_resellers = get_accessible_merchants(current_user.reseller_ids)

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


def apply_merchant_shop_filter(
    current_user: UserInfo,
    requested_reseller_id: Optional[str] = None,
    requested_merchant_identifier: Optional[str] = None,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Apply hierarchical merchant and shop filter based on user's access.

    This function validates access and returns appropriate filters for queries.
    Handles the hierarchy: reseller_ids -> merchant_identifiers

    Args:
        current_user: Current authenticated user
        requested_reseller_id: Specific reseller requested (optional)
        requested_merchant_identifier: Specific shop requested (optional)

    Returns:
        Tuple of (reseller_ids_filter, merchant_identifiers_filter)
        None in either position means no filter needed (access to all)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_resellers = get_accessible_merchants(current_user.reseller_ids)
    accessible_shops = get_accessible_shops(current_user.merchant_identifiers)

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

    # Determine shop filter
    shop_filter = None
    if accessible_shops is not None:
        # User has specific shop access
        if requested_merchant_identifier:
            if requested_merchant_identifier not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {requested_merchant_identifier}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {requested_merchant_identifier}",
                )
            shop_filter = [requested_merchant_identifier]
        else:
            shop_filter = accessible_shops
    else:
        # User has wildcard shop access
        if requested_merchant_identifier:
            shop_filter = [requested_merchant_identifier]
        # else: No filter needed (all shops)

    return (reseller_filter, shop_filter)
