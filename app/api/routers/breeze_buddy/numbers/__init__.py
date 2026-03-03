"""
Modern RESTful outbound number management endpoints with RBAC.

This module provides clean REST API endpoints for managing outbound phone numbers
used for making calls. All write operations require admin access.

Endpoints:
- POST   /numbers           - Create new outbound number (admin only)
- GET    /numbers           - List all outbound numbers (with filters)
- GET    /numbers/{id}      - Get single outbound number by ID
- PUT    /numbers/{id}      - Update outbound number IVR config (admin only)
- DELETE /numbers/{id}      - Disable outbound number (admin only)

For backward compatibility, old endpoints are available in deprecated/outbound_numbers.py
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, Query, status

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import (
    CreateOutboundNumberRequest,
    OutboundNumber,
    UpdateOutboundNumberRequest,
    UserInfo,
)

from .handlers import (
    create_number_handler,
    delete_number_handler,
    get_number_handler,
    list_numbers_handler,
    update_number_handler,
)
from .rbac import filter_numbers_by_rbac, require_admin_access

router = APIRouter()


@router.post(
    "/numbers", response_model=OutboundNumber, status_code=status.HTTP_201_CREATED
)
async def create_outbound_number(
    number: CreateOutboundNumberRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a new outbound phone number.

    Outbound numbers are used by the system to make calls to customers.
    Each number has a provider (TWILIO, EXOTEL) and channel capacity.

    Permissions:
    - Admin only

    Request Body:
        {
            "number": "+1234567890",
            "provider": "EXOTEL",
            "status": "AVAILABLE",
            "maximum_channels": 10
        }

    Returns:
        Created outbound number object with generated ID
    """
    # RBAC: Only admins can create outbound numbers
    require_admin_access(current_user, "create outbound numbers")

    return await create_number_handler(number, current_user)


@router.get("/numbers", response_model=List[OutboundNumber])
async def list_outbound_numbers(
    provider: Optional[str] = Query(
        None, description="Filter by provider (TWILIO, EXOTEL)"
    ),
    status: Optional[str] = Query(
        None, description="Filter by status (AVAILABLE, IN_USE, DISABLED)"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all outbound phone numbers with optional filters.

    Query Parameters:
    - provider: Filter by provider (TWILIO, EXOTEL)
    - status: Filter by status (AVAILABLE, IN_USE, DISABLED)

    Permissions:
    - All authenticated users can view outbound numbers

    Example Requests:
        GET /numbers                          # All numbers
        GET /numbers?provider=EXOTEL          # Filter by provider
        GET /numbers?status=AVAILABLE         # Filter by status

    Returns:
        List of outbound number objects
    """
    # Get numbers
    numbers = await list_numbers_handler(provider, status, current_user)

    # Apply RBAC filtering (currently returns all for authenticated users)
    numbers = filter_numbers_by_rbac(numbers, current_user)

    return numbers


@router.get("/numbers/{number_id}", response_model=OutboundNumber)
async def get_outbound_number(
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get a single outbound number by ID.

    Path Parameters:
    - number_id: Outbound number UUID

    Permissions:
    - All authenticated users can view outbound numbers

    Returns:
        Outbound number object if found
        404 if not found
    """
    return await get_number_handler(number_id, current_user)


@router.put("/numbers/{number_id}", response_model=OutboundNumber)
async def update_outbound_number(
    number_id: str,
    body: UpdateOutboundNumberRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update an outbound number's IVR voice configuration.

    Allows setting IVR-level voice configuration (voice name, greeting,
    goodbye, provider-specific voice settings) that overrides template-level
    IVR settings when handling inbound calls.

    Path Parameters:
    - number_id: Outbound number UUID

    Request Body:
        {
            "ivr_config": {
                "tts_voice_name": "rhea",
                "ivr_greeting": "Welcome. Press 1 for billing, press 2 for support.",
                "ivr_goodbye": "Goodbye.",
                "elevenlabs_voice_configurations": {
                    "voice_id": "voice-id-here",
                    "model_id": "eleven_flash_v2_5"
                }
            }
        }

    Permissions:
    - Admin only

    Returns:
        Updated outbound number object
        404 if number not found
    """
    require_admin_access(current_user, "update outbound numbers")

    return await update_number_handler(number_id, body, current_user)


@router.delete("/numbers/{number_id}", response_model=OutboundNumber)
async def delete_outbound_number(
    number_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Disable an outbound number.

    This performs a soft delete by setting the status to DISABLED.
    The number record is preserved for historical data.

    Path Parameters:
    - number_id: Outbound number UUID to disable

    Permissions:
    - Admin only

    Returns:
        Disabled outbound number object
        404 if number not found
        403 if user lacks permission
    """
    # RBAC: Only admins can delete outbound numbers
    require_admin_access(current_user, "delete outbound numbers")

    return await delete_number_handler(number_id, current_user)
