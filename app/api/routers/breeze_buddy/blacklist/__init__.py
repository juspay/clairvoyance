"""
RESTful blacklist management endpoints.

Endpoints:
- POST   /blacklist                       - Add a phone number to the blacklist
- GET    /blacklist                       - List all blacklisted numbers
- GET    /blacklist/check/{phone_number}  - Check if a number is blacklisted
- DELETE /blacklist/{phone_number}        - Remove a number from the blacklist
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.routers.breeze_buddy.numbers.rbac import require_admin_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import BlacklistedNumber, CreateBlacklistNumberRequest, UserInfo

from .handlers import (
    add_blacklist_handler,
    check_blacklist_handler,
    list_blacklist_handler,
    remove_blacklist_handler,
)

router = APIRouter()


@router.post(
    "/blacklist",
    response_model=BlacklistedNumber,
    status_code=status.HTTP_201_CREATED,
)
async def add_to_blacklist(
    request: CreateBlacklistNumberRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Add a phone number to the blacklist.

    When reseller_id is provided, the number is blocked for that reseller only.
    When reseller_id is omitted, the number is blocked globally for all resellers.

    Permissions:
    - Admin only
    """
    require_admin_access(current_user, "add numbers to blacklist")
    return await add_blacklist_handler(request, current_user)


@router.get("/blacklist", response_model=List[BlacklistedNumber])
async def list_blacklisted_numbers(
    reseller_id: Optional[str] = Query(None, description="Filter by reseller ID"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all blacklisted phone numbers.

    Query Parameters:
    - reseller_id: Optional filter by merchant ID

    Permissions:
    - Admin only
    """
    reseller = reseller_id
    require_admin_access(current_user, "view blacklisted numbers")
    return await list_blacklist_handler(reseller, current_user)


@router.get("/blacklist/check/{phone_number}")
async def check_blacklisted_number(
    phone_number: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Check if a phone number is on the blacklist.

    Returns all blacklist entries for the given phone number.

    Permissions:
    - Admin only
    """
    require_admin_access(current_user, "check blacklisted numbers")
    return await check_blacklist_handler(phone_number, current_user)


@router.delete("/blacklist/{phone_number}")
async def remove_from_blacklist(
    phone_number: str,
    reseller_id: Optional[str] = Query(
        None, description="Reseller ID (omit to remove global blacklist entry)"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Remove a phone number from the blacklist.

    Query Parameters:
    - reseller_id: If provided, removes only the reseller-specific entry.
                   If omitted, removes the global entry.

    Permissions:
    - Admin only
    """
    reseller = reseller_id
    require_admin_access(current_user, "remove numbers from blacklist")
    return await remove_blacklist_handler(phone_number, reseller, current_user)
