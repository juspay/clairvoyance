"""
RBAC (Role-Based Access Control) utilities for templates.
Handles reseller + merchant access control based on JWT token.
"""

from typing import Any, Dict, List, Optional

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import UserInfo


def validate_template_access(
    current_user: UserInfo,
    reseller_id: str,
    merchant_id: Optional[str],
    operation: str = "access",
) -> None:
    """
    Validate user has access to template for given reseller and merchant.

    Args:
        current_user: Current authenticated user with RBAC info
        reseller_id: Reseller ID to validate access for
        merchant_id: Merchant ID to validate access for (optional)
        operation: Operation being performed (for logging)

    Raises:
        HTTPException: 403 if user lacks permission
    """
    # Admin has full access
    if current_user.role == "admin":
        return
    # Check reseller access
    if (
        reseller_id not in current_user.reseller_ids
        and "*" not in current_user.reseller_ids
    ):
        logger.warning(
            f"User {current_user.username} attempted to {operation} template "
            f"for unauthorized reseller: {reseller_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied to reseller {reseller_id}",
        )
    # Check merchant access (if merchant_id is specified)
    if merchant_id:
        if (
            merchant_id not in current_user.merchant_ids
            and "*" not in current_user.merchant_ids
        ):
            logger.warning(
                f"User {current_user.username} attempted to {operation} template "
                f"for unauthorized merchant: {merchant_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
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

    # Wildcard access
    if "*" in current_user.reseller_ids and "*" in current_user.merchant_ids:
        return templates

    filtered = []
    for template in templates:
        # Check reseller access
        has_reseller_access = (
            "*" in current_user.reseller_ids
            or template.reseller_id in current_user.reseller_ids
        )

        # Check merchant access (templates might not have merchant_id)
        if template.merchant_id:
            has_merchant_access = (
                "*" in current_user.merchant_ids
                or template.merchant_id in current_user.merchant_ids
            )
        else:
            # Templates without merchant_id are accessible if reseller access granted
            has_merchant_access = True

        if has_reseller_access and has_merchant_access:
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

    # Reseller owners can manage their own templates
    if reseller_id in current_user.reseller_ids or "*" in current_user.reseller_ids:
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
    Apply hierarchical reseller + merchant filtering based on user's JWT token.

    CRITICAL SECURITY: Always use reseller_ids and merchant_id from JWT token,
    NEVER from request parameters alone.

    Similar to analytics RBAC pattern, this function:
    1. Extracts accessible resellers/merchants from JWT
    2. Validates any reseller/merchant filters in the request
    3. Injects user's accessible resellers/merchants if not specified
    4. Returns 403 if user tries to access unauthorized resources

    Args:
        filters: Request filters (may contain reseller_id/merchant_id)
        current_user: Current authenticated user with RBAC info

    Returns:
        Updated filters with validated reseller/merchant access

    Raises:
        HTTPException: 403 if user tries to access unauthorized resellers/merchants
    """
    # Determine accessible resellers
    if "*" in current_user.reseller_ids:
        accessible_resellers = None  # Wildcard access (admin)
    else:
        accessible_resellers = current_user.reseller_ids

    # Determine accessible merchants
    if "*" in current_user.merchant_ids:
        accessible_merchants = None  # Wildcard access
    else:
        accessible_merchants = current_user.merchant_ids

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

    # Apply merchant filtering
    if accessible_merchants is None:
        # Admin/wildcard merchant access - can access all merchants (within accessible resellers)
        # Keep any merchant filters from request (if user specified specific merchants)
        pass
    else:
        # Non-admin user - enforce merchant access
        # Check if user has no merchant access (empty list)
        if len(accessible_merchants) == 0:
            logger.warning(
                f"User {current_user.username} has no merchant access (empty merchant_ids)"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied: user has no merchant assignments",
            )

        if "merchant_id" in filters:
            # Validate user has access to requested merchant
            if filters["merchant_id"] not in accessible_merchants:
                logger.warning(
                    f"User {current_user.username} attempted to access unauthorized merchant: {filters['merchant_id']}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Access denied to merchant {filters['merchant_id']}",
                )
            # Keep the single merchant_id filter (no change needed)
        else:
            # No merchant filter - apply user's accessible merchants as array
            filters["merchant_ids"] = accessible_merchants

    logger.info(
        f"Applied hierarchical filters for user {current_user.username}: {filters}"
    )

    return filters
