"""
RBAC (Role-Based Access Control) utilities for templates.
Handles merchant + shop access control based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_template_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_identifier: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to template for given reseller and shop.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_identifier: Shop identifier to validate access for (optional)
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return
    reseller = current_user.reseller_ids or current_user.merchant_ids
    # Check reseller access
    if reseller_id not in reseller and "*" not in reseller:
        logger.warning(
            f"User {current_user.username} attempted to {operation} template "
            f"for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )
    # Check shop access (if merchant_identifier is specified)
    if merchant_identifier:
        identifier = current_user.merchant_identifiers or current_user.shop_identifiers
        if merchant_identifier not in identifier and "*" not in identifier:
            logger.warning(
                f"User {current_user.username} attempted to {operation} template "
                f"for unauthorized shop: {merchant_identifier}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to shop {merchant_identifier}",
            )


def filter_templates_by_rbac(templates: List, current_user: UserInfo) -> List:
    """
    Filter templates based on user's RBAC permissions.

    Args:
        templates: List of template objects
        current_user: Current authenticated user

    Returns:
        Filtered list of templates user has access to
    """
    # Admin sees all
    if current_user.role == "admin":
        return templates
    reseller = current_user.reseller_ids or current_user.merchant_ids
    identifier = current_user.merchant_identifiers or current_user.shop_identifiers
    # Wildcard access
    if "*" in reseller and "*" in identifier:
        return templates

    filtered = []
    for template in templates:
        # Check reseller access
        has_reseller_access = "*" in reseller or template.reseller_id in reseller

        # Check shop access (templates might not have merchant_identifier)
        if template.merchant_identifier:
            has_shop_access = (
                "*" in identifier or template.merchant_identifier in identifier
            )
        else:
            # Templates without merchant_identifier are accessible if reseller access granted
            has_shop_access = True

        if has_reseller_access and has_shop_access:
            filtered.append(template)

    return filtered


def require_admin_or_reseller_owner(
    current_user: UserInfo, reseller_id: str, operation: str = "perform this operation"
) -> None:
    """
    Require user to be admin or reseller owner.

    Used for create/update operations on templates.

    Args:
        current_user: Current authenticated user
        reseller_id: Reseller ID being modified
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return
    reseller = current_user.reseller_ids or current_user.merchant_ids
    current_user.merchant_identifiers or current_user.shop_identifiers
    # Reseller owners can manage their own templates
    if reseller_id in reseller or "*" in reseller:
        return

    logger.warning(
        f"User {current_user.username} (role: {current_user.role}) "
        f"attempted to {operation} for unauthorized reseller: {reseller_id}"
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Access denied to {operation} for reseller {reseller_id}",
    )


def apply_hierarchical_template_filters(
    filters: Dict[str, Any], current_user: UserInfo
) -> Dict[str, Any]:
    """
    Apply hierarchical reseller + shop filtering based on user's JWT token.

    CRITICAL SECURITY: Always use reseller_ids and merchant_identifier from JWT token,
    NEVER from request parameters alone.

    Similar to analytics RBAC pattern, this function:
    1. Extracts accessible resellers/shops from JWT
    2. Validates any reseller/shop filters in the request
    3. Injects user's accessible resellers/shops if not specified
    4. Returns 403 if user tries to access unauthorized resources

    Args:
        filters: Request filters (may contain reseller_id/merchant_identifier)
        current_user: Current authenticated user with RBAC info

    Returns:
        Updated filters with validated reseller/shop access

    Raises:
        HTTPException: 403 if user tries to access unauthorized resellers/shops
    """
    reseller = current_user.reseller_ids or current_user.merchant_ids
    identifier = current_user.merchant_identifiers or current_user.shop_identifiers
    # Determine accessible resellers
    if "*" in reseller:
        accessible_resellers = None  # Wildcard access (admin)
    else:
        accessible_resellers = reseller

    # Determine accessible shops
    if "*" in identifier:
        accessible_shops = None  # Wildcard access
    else:
        accessible_shops = identifier

    # Apply reseller filtering
    if accessible_resellers is None:
        # Admin/wildcard access - can access all resellers
        # Keep any reseller filters from request (if admin specified specific resellers)
        pass
    else:
        # Non-admin user - enforce reseller access
        # Check if user has no reseller access (empty list)
        if len(accessible_resellers) == 0:
            logger.warning(
                f"User {current_user.username} has no reseller access (empty reseller_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no reseller assignments",
            )

        if "reseller_id" in filters:
            # Validate user has access to requested reseller
            if filters["reseller_id"] not in accessible_resellers:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized reseller: {filters['reseller_id']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to reseller {filters['reseller_id']}",
                )
            # Keep the single reseller_id filter (no change needed)
        else:
            # No reseller filter - apply user's accessible resellers as array
            filters["reseller_ids"] = accessible_resellers

    # Apply shop filtering
    if accessible_shops is None:
        # Admin/wildcard shop access - can access all shops (within accessible resellers)
        # Keep any shop filters from request (if user specified specific shops)
        pass
    else:
        # Non-admin user - enforce shop access
        # Check if user has no shop access (empty list)
        if len(accessible_shops) == 0:
            logger.warning(
                f"User {current_user.username} has no shop access (empty merchant_identifiers)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no shop assignments",
            )

        if "merchant_identifier" in filters:
            # Validate user has access to requested shop
            if filters["merchant_identifier"] not in accessible_shops:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized shop: {filters['merchant_identifier']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to shop {filters['merchant_identifier']}",
                )
            # Keep the single merchant_identifier filter (no change needed)
        else:
            # No shop filter - apply user's accessible shops as array
            filters["merchant_identifiers"] = accessible_shops

    logger.info(
        f"Applied hierarchical filters for user {current_user.username}: {filters}"
    )

    return filters
