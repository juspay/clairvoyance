"""
Breeze Buddy specific authorization logic for shop access control.
Implements multi-shop RBAC authorization based on merchant_ids and shop_identifiers.
"""

from typing import List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


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


def get_accessible_shops(shop_identifiers: List[str]) -> Optional[List[str]]:
    """
    Returns list of accessible shops, or None if access to ALL shops.

    Args:
        shop_identifiers: shop_identifiers array from JWT token

    Returns:
        None if user has access to ALL shops (["*"])
        List[str] of specific shop_identifiers otherwise
    """
    if "*" in shop_identifiers:
        return None  # None means "all shops"
    else:
        return shop_identifiers


def validate_shop_access(
    current_user: UserInfo,
    shop_identifier: Optional[str] = None,
    shop_identifiers: Optional[List[str]] = None,
) -> None:
    """
    Validate if current user has access to requested shop(s).

    Args:
        current_user: Current authenticated user
        shop_identifier: Single shop identifier to check (optional)
        shop_identifiers: Multiple shop identifiers to check (optional)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    # User has access to all shops (admin/reseller with wildcard)
    if accessible_shops is None:
        return  # Allow access

    # Check single shop access
    if shop_identifier:
        if shop_identifier not in accessible_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shop: {shop_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {shop_identifier}",
            )

    # Check multiple shops access
    if shop_identifiers:
        unauthorized_shops = [
            shop for shop in shop_identifiers if shop not in accessible_shops
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
    requested_shop_identifier: Optional[str] = None,
    requested_shop_identifiers: Optional[List[str]] = None,
) -> Optional[List[str]]:
    """
    Apply shop filter based on user's accessible shops.

    This function validates access and returns the appropriate shop filter to use in queries.

    Args:
        current_user: Current authenticated user
        requested_shop_identifier: Single shop requested by user (optional)
        requested_shop_identifiers: Multiple shops requested by user (optional)

    Returns:
        None if user has access to ALL shops (no filter needed)
        List[str] of shop identifiers to filter by

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    # User has access to all shops (admin/reseller with wildcard)
    if accessible_shops is None:
        # If user requested specific shop(s), return that
        if requested_shop_identifier:
            return [requested_shop_identifier]
        if requested_shop_identifiers:
            return requested_shop_identifiers
        # Otherwise, no filter (return all shops)
        return None

    # User has access to specific shops only
    # Validate requested shops
    if requested_shop_identifier:
        if requested_shop_identifier not in accessible_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shop: {requested_shop_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {requested_shop_identifier}",
            )
        return [requested_shop_identifier]

    if requested_shop_identifiers:
        unauthorized_shops = [
            shop for shop in requested_shop_identifiers if shop not in accessible_shops
        ]

        if unauthorized_shops:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized shops: {unauthorized_shops}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shops: {unauthorized_shops}",
            )
        return requested_shop_identifiers

    # No specific shops requested, return user's accessible shops
    return accessible_shops


def filter_by_shop_access(
    current_user: UserInfo, data_list: List[dict], shop_key: str = "shop_identifier"
) -> List[dict]:
    """
    Filter a list of data by user's accessible shops.

    Args:
        current_user: Current authenticated user
        data_list: List of dictionaries containing data
        shop_key: Key name for shop identifier in each dict (default: "shop_identifier")

    Returns:
        Filtered list containing only accessible shops
    """
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    # User has access to all shops
    if accessible_shops is None:
        return data_list

    # Filter by accessible shops
    return [
        item
        for item in data_list
        if shop_key in item and item[shop_key] in accessible_shops
    ]


def has_wildcard_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all shops) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard access, False otherwise
    """
    return "*" in current_user.shop_identifiers


def has_wildcard_merchant_access(current_user: UserInfo) -> bool:
    """
    Check if user has wildcard (all merchants) access.

    Args:
        current_user: Current authenticated user

    Returns:
        True if user has wildcard merchant access, False otherwise
    """
    return "*" in current_user.merchant_ids


def validate_merchant_access(
    current_user: UserInfo,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = None,
) -> None:
    """
    Validate if current user has access to requested merchant(s).

    Args:
        current_user: Current authenticated user
        merchant_id: Single merchant ID to check (optional)
        merchant_ids: Multiple merchant IDs to check (optional)

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
            m for m in merchant_ids if m not in accessible_merchants
        ]

        if unauthorized_merchants:
            logger.warning(
                f"User {current_user.username} attempted to access unauthorized merchants: {unauthorized_merchants}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to one or more requested merchants: {unauthorized_merchants}",
            )


def apply_merchant_shop_filter(
    current_user: UserInfo,
    requested_merchant_id: Optional[str] = None,
    requested_shop_identifier: Optional[str] = None,
) -> tuple[Optional[List[str]], Optional[List[str]]]:
    """
    Apply hierarchical merchant and shop filter based on user's access.

    This function validates access and returns appropriate filters for queries.
    Handles the hierarchy: merchant_ids -> shop_identifiers

    Args:
        current_user: Current authenticated user
        requested_merchant_id: Specific merchant requested (optional)
        requested_shop_identifier: Specific shop requested (optional)

    Returns:
        Tuple of (merchant_ids_filter, shop_identifiers_filter)
        None in either position means no filter needed (access to all)

    Raises:
        HTTPException: 403 Forbidden if user doesn't have access
    """
    accessible_merchants = get_accessible_merchants(current_user.merchant_ids)
    accessible_shops = get_accessible_shops(current_user.shop_identifiers)

    # Determine merchant filter
    merchant_filter = None
    if accessible_merchants is not None:
        # User has specific merchant access
        if requested_merchant_id:
            if requested_merchant_id not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {requested_merchant_id}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {requested_merchant_id}",
                )
            merchant_filter = [requested_merchant_id]
        else:
            merchant_filter = accessible_merchants
    else:
        # User has wildcard merchant access
        if requested_merchant_id:
            merchant_filter = [requested_merchant_id]
        # else: No filter needed (all merchants)

    # Determine shop filter
    shop_filter = None
    if accessible_shops is not None:
        # User has specific shop access
        if requested_shop_identifier:
            if requested_shop_identifier not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {requested_shop_identifier}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {requested_shop_identifier}",
                )
            shop_filter = [requested_shop_identifier]
        else:
            shop_filter = accessible_shops
    else:
        # User has wildcard shop access
        if requested_shop_identifier:
            shop_filter = [requested_shop_identifier]
        # else: No filter needed (all shops)

    return (merchant_filter, shop_filter)
