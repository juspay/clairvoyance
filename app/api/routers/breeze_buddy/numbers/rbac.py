"""
RBAC (Role-Based Access Control) utilities for telephony numbers.
Only admin users can manage numbers; reads are scoped to ownership.
"""

from typing import List, Optional, Set, Tuple

from fastapi import HTTPException, status

from app.core.logger import logger
from app.schemas import TelephonyNumber, UserInfo


def require_admin_access(
    current_user: UserInfo, operation: str = "perform this operation"
) -> None:
    """
    Validate user is an admin.

    Telephony numbers are system-wide resources that require admin access.

    Args:
        current_user: Current authenticated user
        operation: Operation being performed (for error message)

    Raises:
        HTTPException: 403 if user is not admin
    """
    if current_user.role != "admin":
        logger.warning(
            f"Non-admin user {current_user.username} (role: {current_user.role}) "
            f"attempted to {operation}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin access required to {operation}",
        )


def require_number_in_tenant_scope(
    number: TelephonyNumber,
    template_reseller_id: Optional[str],
    template_merchant_id: Optional[str],
) -> None:
    """
    A template may pin (template.telephony_number_id) only:
      - shared-pool numbers (no owner),
      - numbers owned by the template's own merchant, or
      - numbers owned by the template's umbrella.

    Anything else is a cross-tenant pin and is rejected — for every caller,
    admins included: an admin who really wants the pin re-assigns the number
    first, so ownership metadata never silently diverges from usage again.

    Applied to NEW or CHANGED pins only; existing rows are grandfathered
    until the ownership backfill cleans them up (the picker logs a warning
    for grandfathered cross-merchant pins at call time).

    Raises:
        HTTPException: 400 when the number belongs to another tenant
    """
    # Error details carry number.id, never the raw phone number (PII stays
    # out of wire responses; resolve the id via GET /numbers/{id}).
    if number.merchant_id is not None:
        if number.merchant_id != template_merchant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Number {number.id} is provisioned for merchant "
                    f"'{number.merchant_id}' and cannot be pinned by a "
                    f"template of '{template_merchant_id}'. Use a shared "
                    "number, one of this merchant's own numbers, or "
                    "re-assign the number first."
                ),
            )
        return
    if number.reseller_id is not None and number.reseller_id != template_reseller_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Number {number.id} belongs to the "
                f"'{number.reseller_id}' umbrella and cannot be pinned by a "
                f"template under '{template_reseller_id}'."
            ),
        )


def rbac_number_scopes(current_user: UserInfo) -> Tuple[Set[str], Set[str]]:
    """
    The (merchant_scope, reseller_scope) sets a caller's number visibility is
    computed from. Ownership visibility is strictly downward:

    - reseller role: umbrella-wide — sees umbrella-owned numbers AND every
      merchant-owned number under the umbrella (merchant-owned rows carry the
      auto-filled reseller_id).
    - merchant/user roles: merchant-only — reseller_ids in their JWT grant
      workspace membership, NOT umbrella-wide number access, so the umbrella
      scope is emptied. Without this, a merchant user would see sibling
      merchants' numbers through the shared umbrella id.

    Wildcards are dropped from both sets (a '*' must never widen visibility).
    Admins bypass scoping entirely and never reach this helper's output.
    """
    m_ids = {m for m in (current_user.merchant_ids or []) if m != "*"}
    if current_user.role == "reseller":
        r_ids = {r for r in (current_user.reseller_ids or []) if r != "*"}
    else:
        r_ids = set()
    return m_ids, r_ids


def number_in_rbac_scope(
    number_id: str,
    merchant_id: Optional[str],
    reseller_id: Optional[str],
    merchant_scope: Set[str],
    reseller_scope: Set[str],
    pinned_number_ids: Set[str],
) -> bool:
    """
    The single visibility rule for a telephony number: owned by one of the
    caller's merchants, owned by one of their umbrellas (reseller role only —
    build the scopes with rbac_number_scopes), or pinned by one of their
    templates. Shared by the /numbers list filter and the analytics handler
    so the two can never drift.
    """
    return (
        (merchant_id is not None and merchant_id in merchant_scope)
        or (reseller_id is not None and reseller_id in reseller_scope)
        or number_id in pinned_number_ids
    )


def filter_numbers_by_rbac(
    numbers: List[TelephonyNumber],
    current_user: UserInfo,
    pinned_number_ids: Optional[List[str]] = None,
) -> List[TelephonyNumber]:
    """
    Scope the numbers list to what the caller may see.

    - admin: everything (the fleet view).
    - reseller: numbers owned by their umbrella — including merchant-owned
      numbers under it — plus their templates' pins.
    - merchant/user: numbers owned by one of their merchants plus
      `pinned_number_ids` — the ids their accessible templates pin via
      template.telephony_number_id (so a merchant dialing from a shared-pool
      number still sees THAT number, without the whole shared fleet leaking
      cross-tenant). Umbrella-owned numbers are NOT visible to merchants:
      the reseller_ids their JWT carries mean workspace membership, not
      umbrella-wide access.
    """
    if current_user.role == "admin":
        return numbers

    m_ids, r_ids = rbac_number_scopes(current_user)
    pinned = set(pinned_number_ids or [])

    visible = [
        n
        for n in numbers
        if number_in_rbac_scope(
            n.id, n.merchant_id, n.reseller_id, m_ids, r_ids, pinned
        )
    ]
    logger.info(
        f"RBAC-filtered numbers for {current_user.username} "
        f"(role: {current_user.role}): {len(visible)}/{len(numbers)} visible"
    )
    return visible


def narrow_numbers_to_workspace(
    numbers: List[TelephonyNumber],
    merchant_id: str,
    pinned_number_ids: List[str],
) -> List[TelephonyNumber]:
    """
    View-as narrowing for the console's workspace switcher: reduce an
    already-RBAC-filtered list to exactly what a user of `merchant_id` would
    see — numbers owned by that merchant plus the ids its templates pin.

    This only ever narrows (it runs AFTER filter_numbers_by_rbac), so a
    caller can never widen their scope by passing someone else's merchant_id:
    an admin narrows the fleet, a reseller narrows their umbrella, and a
    merchant intersecting with their own scope is a no-op or smaller.
    """
    pinned = set(pinned_number_ids)
    return [n for n in numbers if n.merchant_id == merchant_id or n.id in pinned]


def narrow_numbers_to_umbrella(
    numbers: List[TelephonyNumber],
    reseller_id: str,
    pinned_number_ids: List[str],
) -> List[TelephonyNumber]:
    """
    Umbrella flavor of narrow_numbers_to_workspace: reduce to the reseller
    view — umbrella-owned rows AND merchant-owned rows under it (both carry
    reseller_id) plus the umbrella templates' pins. Narrowing-only, same as
    the merchant variant.
    """
    pinned = set(pinned_number_ids)
    return [n for n in numbers if n.reseller_id == reseller_id or n.id in pinned]


def may_view_as(
    current_user: UserInfo,
    workspace_merchant_id: Optional[str] = None,
    workspace_reseller_id: Optional[str] = None,
) -> bool:
    """
    Explicit gate for the view-as narrowing params: may this caller even ASK
    for that workspace's view? Admins may view any workspace; everyone else
    only workspaces already inside their JWT scope ('*' = unrestricted).

    Defense in depth, not a standalone guarantee: the no-widening property
    comes from the narrowing being an intersection applied AFTER
    filter_numbers_by_rbac. This gate adds an independent membership check
    on top, but it is deliberately permissive for admins and for '*'
    wildcard scopes — for those callers it passes ANY workspace id, so the
    intersection ordering is still load-bearing. Keep the narrowing after
    the RBAC filter.
    """
    if current_user.role == "admin":
        return True
    if workspace_merchant_id is not None:
        merchants = current_user.merchant_ids or []
        return "*" in merchants or workspace_merchant_id in merchants
    if workspace_reseller_id is not None:
        resellers = current_user.reseller_ids or []
        return "*" in resellers or workspace_reseller_id in resellers
    return True
