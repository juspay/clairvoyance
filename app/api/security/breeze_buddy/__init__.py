"""
Breeze Buddy specific security and authorization utilities.
"""

from .authorization import (
    apply_merchant_shop_filter,
    apply_shop_filter,
    filter_by_shop_access,
    get_accessible_merchants,
    get_accessible_shops,
    has_wildcard_access,
    has_wildcard_merchant_access,
    validate_merchant_access,
    validate_shop_access,
)
from .rbac_token import (
    get_active_user,
    get_current_user_with_rbac,
    rbac_token_manager,
)

__all__ = [
    # Merchant authorization
    "get_accessible_merchants",
    "validate_merchant_access",
    "has_wildcard_merchant_access",
    # Shop authorization
    "get_accessible_shops",
    "validate_shop_access",
    "apply_shop_filter",
    "filter_by_shop_access",
    "has_wildcard_access",
    # Hierarchical merchant + shop authorization
    "apply_merchant_shop_filter",
    # RBAC token management
    "rbac_token_manager",
    "get_current_user_with_rbac",
    "get_active_user",
]
