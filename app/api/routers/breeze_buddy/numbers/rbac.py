"""
RBAC (Role-Based Access Control) utilities for telephony numbers.
Only admin users can manage numbers; reads are scoped to ownership.
"""

from typing import List, Optional, Set, Tuple

from fastapi import HTTPException, status

from app.core.logger import logger
from app.database.accessor.breeze_buddy.merchants import (
    get_merchant_by_merchant_identifier,
)
from app.schemas import TelephonyNumber, UserInfo

# NOTE: number visibility has exactly one enforcement point —
# ``filter_numbers_by_rbac``, applied by the router to both the list and the
# single-number read. A second coarse gate used to live here
# (``validate_number_access``); it was removed because it was pin-blind (it
# denied a merchant the shared-pool number its own template pins) and answered
# 403 where the router answers 404, leaking existence. Do not reintroduce a
# parallel check: add to ``number_in_rbac_scope`` instead, so one rule governs.


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


def require_admin_or_reseller_access(
    current_user: UserInfo, operation: str = "perform this operation"
) -> None:
    """
    Validate user is an admin or reseller.

    Gates provider number search/buy: purchasing spends real money, so it is
    restricted to admin and reseller for now, not the full set resolve_buy_scope
    is otherwise able to handle (merchant/user). resolve_buy_scope's
    merchant/user branch stays in place, unused while this gate is active, so
    widening access later is a one-line change here rather than new logic.

    Raises:
        HTTPException: 403 if user is neither admin nor reseller
    """
    if current_user.role not in ("admin", "reseller"):
        logger.warning(
            f"User {current_user.username} (role: {current_user.role}) "
            f"attempted to {operation}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin or reseller access required to {operation}",
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


async def resolve_ownership(
    merchant_id: Optional[str], reseller_id: Optional[str]
) -> Tuple[Optional[str], Optional[str]]:
    """
    Validate an ownership pair and auto-fill the umbrella for merchant-owned
    numbers from merchants.reseller_id. Raises 400 on an unknown merchant.

    Shared by manual number provisioning and the provider buy flow so both
    paths land on identical ownership metadata for the same merchant_id.
    """
    if merchant_id:
        merchant = await get_merchant_by_merchant_identifier(merchant_id)
        if not merchant:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown merchant_id: {merchant_id}",
            )
        return merchant_id, reseller_id or merchant.reseller_id
    return None, reseller_id


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


async def resolve_buy_scope(
    current_user: UserInfo,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """
    Resolve and validate (reseller_id, merchant_id) ownership for a number
    purchase, scoped to the caller's own role. Runs BEFORE anything is
    purchased -- a rejection here must never reach the provider.

    Returns (reseller_id, merchant_id) -- note this is the OPPOSITE order from
    resolve_ownership (which returns (merchant_id, reseller_id)); every branch
    below flips it explicitly so this function's return order always matches
    its own parameter order.

    - admin: trusted as given. merchant_id, if any, must be a real merchant
      (resolve_ownership); reseller_id auto-fills from it when omitted.
    - reseller: reseller_id is always their own umbrella -- never a client
      choice. merchant_id, if given, must be one of the merchants currently
      under that umbrella (rbac_number_scopes, so this can never drift from
      what GET /numbers considers "theirs"). Omitting merchant_id buys an
      umbrella-owned (not merchant-specific) number.
    - merchant/user: merchant_id must be one of their own. Exactly one in
      scope and omitted -> used automatically (the common case). More than
      one and omitted -> 400, never guess. reseller_id is always derived
      from the chosen merchant's own record, never taken from the caller.

    Raises:
        HTTPException: 400 for missing/ambiguous/unknown input, 403 for
            anything outside the caller's own scope.
    """
    if current_user.role == "admin":
        if not reseller_id and not merchant_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="reseller_id or merchant_id is required.",
            )
        resolved_merchant_id, resolved_reseller_id = await resolve_ownership(
            merchant_id, reseller_id
        )
        return resolved_reseller_id, resolved_merchant_id

    merchant_scope, reseller_scope = rbac_number_scopes(current_user)

    if current_user.role == "reseller":
        if reseller_id and reseller_id not in reseller_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to reseller {reseller_id}",
            )
        own_reseller_id = reseller_id or (
            next(iter(reseller_scope)) if len(reseller_scope) == 1 else None
        )
        if not own_reseller_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine your reseller scope.",
            )

        if not merchant_id:
            return own_reseller_id, None  # umbrella-owned, no specific merchant

        if merchant_id not in merchant_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
            )
        resolved_merchant_id, resolved_reseller_id = await resolve_ownership(
            merchant_id, own_reseller_id
        )
        return resolved_reseller_id, resolved_merchant_id

    # merchant / user roles: merchant-only scope, reseller_id always derived
    # from the resolved merchant -- never taken from the caller directly.
    if merchant_id:
        if merchant_id not in merchant_scope:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied to merchant {merchant_id}",
            )
    elif len(merchant_scope) == 1:
        merchant_id = next(iter(merchant_scope))
    elif not merchant_scope:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You have no merchant scope to buy a number for.",
        )
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You have access to multiple merchants; specify merchant_id.",
        )

    resolved_merchant_id, resolved_reseller_id = await resolve_ownership(
        merchant_id, None
    )
    return resolved_reseller_id, resolved_merchant_id
