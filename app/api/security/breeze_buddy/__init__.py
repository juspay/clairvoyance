"""
Breeze Buddy specific security and authorization utilities.
"""

from .authorization import (
    apply_merchant_filter,
    apply_reseller_merchant_filter,
    filter_by_merchant_access,
    get_accessible_merchants,
    get_accessible_resellers,
    has_wildcard_access,
    has_wildcard_reseller_access,
    validate_merchant_access,
    validate_reseller_access,
)
from .rbac_token import (
    get_active_user,
    get_current_user_with_rbac,
    rbac_token_manager,
)

__all__ = [
    # reseller authorization
    "get_accessible_resellers",
    "validate_reseller_access",
    "has_wildcard_reseller_access",
    # Merchant authorization
    "get_accessible_merchants",
    "validate_merchant_access",
    "apply_merchant_filter",
    "filter_by_merchant_access",
    "has_wildcard_access",
    # Hierarchical reseller + merchant authorization
    "apply_reseller_merchant_filter",
    # RBAC token management
    "rbac_token_manager",
    "get_current_user_with_rbac",
    "get_active_user",
]
