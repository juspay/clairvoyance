"""
Modern RESTful lead management endpoints with RBAC.

This module provides clean REST API endpoints for managing leads (call requests).
Leads represent individual call attempts to customers.

Endpoints:
- POST   /leads              - Push new lead for processing
- GET    /leads/{id}         - Get lead details by ID

For backward compatibility, old endpoints are available in deprecated/leads.py
"""

from fastapi import APIRouter, Depends, HTTPException, status

from app.ai.voice.agents.breeze_buddy.types.models import PushLeadRequest
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo

from .handlers import (
    get_lead_handler,
    push_lead_handler,
)
from .rbac import (
    validate_lead_access,
    validate_lead_read_access,
)

router = APIRouter()


@router.post("/leads", status_code=status.HTTP_201_CREATED)
async def push_lead(
    req: PushLeadRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Push a new lead for processing.

    This endpoint receives lead details (customer info, order data, etc.) and queues
    them for automated call processing. The system will:
    1. Validate the payload against the template's expected schema
    2. Get the call execution configuration
    3. Schedule the call based on initial_offset
    4. Return a lead_call_tracker_id for tracking

    Permissions:
    - Admin: Can push leads for any merchant/shop
    - Merchant: Can push leads for own shops only

    Request Body:
        {
            "merchant": "shop_123",
            "template": "order-confirmation",
            "identifier": "shop_123",
            "request_id": "order_456",
            "reporting_webhook_url": "https://example.com/webhook",
            "payload": {
                "customer_name": "John Doe",
                "customer_mobile_number": "+1234567890",
                "shop_name": "My Shop",
                "total_price": "100.00",
                "items": [...]
            }
        }

    Returns:
        {
            "status": "queued",
            "lead_call_tracker_id": "uuid",
            "order_id": "order_456",
            "message": "Call request added to queue for processing"
        }
    """
    # RBAC: Check permission to push leads for this merchant/shop
    validate_lead_access(
        current_user,
        req.merchant,
        req.identifier,
        operation="push leads for"
    )

    return await push_lead_handler(req, current_user)


@router.get("/leads/{lead_id}")
async def get_lead(
    lead_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get lead details by ID.

    Returns lead information excluding sensitive fields like metadata,
    cost, lock status, and internal timestamps.

    Path Parameters:
    - lead_id: Lead UUID

    RBAC:
    - Admin: Can access any lead
    - Merchant: Can only access leads for own merchants/shops

    Returns:
        Lead object (sanitized) if found
        404 if not found or access denied
    """
    # Get lead from handler
    from app.database.accessor import get_lead_by_id
    lead = await get_lead_by_id(lead_id)

    if not lead:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Lead not found for ID: {lead_id}"
        )

    # RBAC: Check access (returns 404 to avoid leaking existence)
    validate_lead_read_access(current_user, lead, operation="access")

    # Get sanitized lead data
    return await get_lead_handler(lead_id, current_user)
