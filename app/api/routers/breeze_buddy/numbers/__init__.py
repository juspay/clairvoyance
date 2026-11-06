"""
Modern RESTful telephony number management endpoints with RBAC.

This module provides clean REST API endpoints for managing telephony phone
numbers — outbound caller IDs and inbound DIDs. All write operations require
admin access; reads are scoped to ownership.

Ownership model (telephony_numbers.reseller_id / merchant_id):
- merchant_id set          → merchant-owned (preferred by the dispatcher for
                             that merchant's calls)
- reseller_id only         → umbrella-owned
- both NULL                → shared platform pool (legacy fallback the
                             dispatcher scans when a template pins no number)

Endpoints:
- POST   /numbers           - Provision a number (admin only)
- GET    /numbers           - List numbers in the caller's scope
- GET    /numbers/{id}      - Get single number by ID (scoped)
- PATCH  /numbers/{id}      - Update ownership / status / capacity (admin only)
- DELETE /numbers/{id}      - Disable a number (admin only)

For backward compatibility, old endpoints are available in deprecated/telephony_numbers.py
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.database.accessor import get_template_pinned_number_ids
from app.schemas import (
    CallProvider,
    CreateTelephonyNumberRequest,
    TelephonyNumber,
    UpdateTelephonyNumberRequest,
    UserInfo,
)
from app.schemas.breeze_buddy.telephony_numbers import (
    TelephonyNumberBuyRequest,
    TelephonyNumberBuyResponse,
    TelephonyNumberSearchParams,
    TelephonyNumberSearchResponse,
)

from .handlers import (
    create_number_handler,
    delete_number_handler,
    get_number_handler,
    list_numbers_handler,
    update_number_handler,
)
from .provider_handlers import (
    buy_provider_number_handler,
    search_provider_numbers_handler,
)
from .rbac import (
    filter_numbers_by_rbac,
    may_view_as,
    narrow_numbers_to_umbrella,
    narrow_numbers_to_workspace,
    rbac_number_scopes,
    require_admin_access,
    require_admin_or_reseller_access,
)

router = APIRouter()


def parse_call_provider(provider_name: str) -> CallProvider:
    """Parse provider path values case-insensitively."""
    try:
        return CallProvider(provider_name.upper())
    except ValueError:
        supported_providers = ", ".join(provider.value for provider in CallProvider)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported provider '{provider_name}'. "
                f"Supported providers: {supported_providers}"
            ),
        )


async def _pinned_ids_for(current_user: UserInfo) -> List[str]:
    """
    Template-pinned number ids for a non-admin caller's scope. Uses the same
    role-gated scopes as visibility: merchants get their own templates' pins
    only, resellers get every pin under their umbrella.
    """
    if current_user.role == "admin":
        return []
    m_ids, r_ids = rbac_number_scopes(current_user)
    return await get_template_pinned_number_ids(list(m_ids), list(r_ids))


@router.post(
    "/numbers", response_model=TelephonyNumber, status_code=status.HTTP_201_CREATED
)
async def create_telephony_number(
    number: CreateTelephonyNumberRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Provision a new telephony number.

    Ownership is an explicit choice: pass merchant_id (merchant-owned;
    reseller_id auto-fills from the merchant's umbrella), reseller_id only
    (umbrella-owned), or shared_pool=true (shared platform pool).

    Permissions:
    - Admin only

    Request Body:
        {
            "number": "+1234567890",
            "provider": "EXOTEL",
            "status": "AVAILABLE",
            "maximum_channels": 10,
            "merchant_id": "acme.myshopify.com"
        }

    Returns:
        Created telephony number object with generated ID
    """
    require_admin_access(current_user, "create telephony numbers")

    return await create_number_handler(number, current_user)


@router.get("/numbers", response_model=List[TelephonyNumber])
async def list_telephony_numbers(
    provider: Optional[str] = Query(
        None, description="Filter by provider (TWILIO, EXOTEL, PLIVO)"
    ),
    status: Optional[str] = Query(
        None, description="Filter by status (AVAILABLE, IN_USE, DISABLED)"
    ),
    merchant_id: Optional[str] = Query(
        None,
        description=(
            "Workspace view-as filter: narrow the result to what a user of "
            "this merchant would see (owned + template-pinned numbers). "
            "Narrowing only — never widens the caller's own scope."
        ),
    ),
    reseller_id: Optional[str] = Query(
        None,
        description=(
            "Umbrella view-as filter: narrow to the reseller view (umbrella-"
            "owned + merchant-owned under it + umbrella pins). merchant_id "
            "wins when both are passed. Narrowing only."
        ),
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List telephony numbers in the caller's scope, with optional filters.

    Query Parameters:
    - provider: Filter by provider (TWILIO, EXOTEL, PLIVO)
    - status: Filter by status (AVAILABLE, IN_USE, DISABLED)
    - merchant_id: Narrow to one workspace's view (owned + pinned) — used by
      the console's workspace switcher so admins/resellers see exactly what
      that merchant's users see.

    Permissions:
    - Admin sees the whole fleet; everyone else sees numbers owned by their
      merchants/umbrellas plus the numbers their templates dial from.
      merchant_id then intersects that scope, so it can only narrow.

    Returns:
        List of telephony number objects
    """
    numbers = await list_numbers_handler(provider, status, current_user)

    pinned = await _pinned_ids_for(current_user)
    numbers = filter_numbers_by_rbac(numbers, current_user, pinned)

    if merchant_id:
        if not may_view_as(current_user, workspace_merchant_id=merchant_id):
            return []
        workspace_pins = await get_template_pinned_number_ids([merchant_id], [])
        numbers = narrow_numbers_to_workspace(numbers, merchant_id, workspace_pins)
    elif reseller_id:
        if not may_view_as(current_user, workspace_reseller_id=reseller_id):
            return []
        umbrella_pins = await get_template_pinned_number_ids([], [reseller_id])
        numbers = narrow_numbers_to_umbrella(numbers, reseller_id, umbrella_pins)

    return numbers


@router.get(
    "/numbers/{provider_name}/search", response_model=TelephonyNumberSearchResponse
)
async def search_provider_numbers_endpoint(
    provider_name: str,
    country_iso: str = Query(
        default="IN",
        description="ISO 3166 alpha-2 country code",
    ),
    type: Optional[str] = Query(
        default=None,
        description="Number type: tollfree, local, mobile, national, fixed",
    ),
    pattern: Optional[str] = Query(
        default=None,
        description="Number pattern to match (e.g., '022' for Mumbai)",
    ),
    services: Optional[str] = Query(
        default=None,
        description="Filter by capabilities: voice, sms, voice,sms",
    ),
    region: Optional[str] = Query(
        default=None,
        description="Region name (e.g., 'Mumbai'). For fixed type only.",
    ),
    limit: int = Query(default=20, ge=1, le=20, description="Results per page"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Search available phone numbers from a specific provider's inventory.

    Example Requests:
        GET /numbers/PLIVO/search?country_iso=IN&type=fixed&pattern=022
        GET /numbers/TWILIO/search?country_iso=US&type=mobile

    Permissions:
    - Admin or reseller only.

    Returns:
        List of available numbers with pricing and metadata
    """
    provider = parse_call_provider(provider_name)
    require_admin_or_reseller_access(current_user, f"search {provider.value} numbers")

    params = TelephonyNumberSearchParams(
        country_iso=country_iso,
        type=type,
        pattern=pattern,
        services=services,
        region=region,
        limit=limit,
        offset=offset,
    )
    return await search_provider_numbers_handler(provider, params, current_user)


@router.post(
    "/numbers/{provider_name}/buy",
    response_model=TelephonyNumberBuyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def buy_provider_number_endpoint(
    provider_name: str,
    request: TelephonyNumberBuyRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Buy a phone number from a provider and register it as a telephony number.

    Atomic operation:
    1. Purchases the number from the Provider
    2. Registers it in the telephony_numbers table
    3. On DB failure, attempts best-effort unrent from Provider

    Request Body:
        {
            "number": "912212345678",
            "reseller_id": "reseller_acme",
            "merchant_id": "merchant_xyz",  // optional
            "maximum_channels": 10
        }

    Permissions:
    - Admin or reseller only. resolve_buy_scope additionally restricts a
      reseller to their own umbrella (and merchants under it), never another
      tenant's -- see numbers/rbac.py.

    Returns:
        Provider purchase status + created telephony_number record
    """
    provider = parse_call_provider(provider_name)
    require_admin_or_reseller_access(current_user, f"buy {provider.value} numbers")

    return await buy_provider_number_handler(provider, request, current_user)


@router.get("/numbers/{number_id}", response_model=TelephonyNumber)
async def get_telephony_number(
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get a single telephony number by ID.

    Path Parameters:
    - number_id: Telephony number UUID

    Permissions:
    - Same visibility rule as the list endpoint (owned or template-pinned);
      out-of-scope numbers 404 rather than leak.

    Returns:
        Telephony number object if found and visible
        404 if not found or out of scope
    """
    number = await get_number_handler(number_id, current_user)

    pinned = await _pinned_ids_for(current_user)
    if not filter_numbers_by_rbac([number], current_user, pinned):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Telephony number {number_id} not found",
        )
    return number


@router.patch("/numbers/{number_id}", response_model=TelephonyNumber)
async def update_telephony_number(
    number_id: str,
    update: UpdateTelephonyNumberRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update a telephony number: assign it to a merchant/umbrella, release it
    back to the shared pool (clear_ownership), change status, or resize
    maximum_channels (the channel-token reconciler re-syncs the Redis
    semaphore on its next pass).

    Permissions:
    - Admin only

    Returns:
        Updated telephony number object
        404 if number not found
    """
    require_admin_access(current_user, "update telephony numbers")

    return await update_number_handler(number_id, update, current_user)


@router.delete("/numbers/{number_id}", response_model=TelephonyNumber)
async def delete_telephony_number(
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Disable a telephony number.

    This performs a soft delete by setting the status to DISABLED.
    The number record is preserved for historical data.

    Path Parameters:
    - number_id: Telephony number UUID to disable

    Permissions:
    - Admin only

    Returns:
        Disabled telephony number object
        404 if number not found
        403 if user lacks permission
    """
    require_admin_access(current_user, "delete telephony numbers")

    return await delete_number_handler(number_id, current_user)
