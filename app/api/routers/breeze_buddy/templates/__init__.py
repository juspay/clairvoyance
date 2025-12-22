"""
Modern RESTful template management endpoints with RBAC.

This module provides clean REST API endpoints for managing call flow templates.
Templates define the conversational flow for automated calls.

Endpoints:
- POST   /templates           - Create new template
- GET    /templates           - Get templates (filtered by merchant/shop/name)

For backward compatibility, old endpoints are available in deprecated/template.py
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.ai.voice.agents.breeze_buddy.template.types import CreateTemplateRequest
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo

from .handlers import (
    create_template_handler,
    get_template_handler,
)
from .rbac import (
    require_admin_or_merchant_owner,
    validate_template_access,
)

router = APIRouter()


@router.post("/templates", status_code=status.HTTP_201_CREATED)
async def create_template(
    template_data: CreateTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Create a complete template from a JSON object.

    Templates define conversational flows for automated calls using a node-based system.
    Each template contains:
    - Flow structure (nodes, initial_node, etc.)
    - Expected payload schema
    - Expected callback response schema

    Permissions:
    - Admin: Can create templates for any merchant
    - Merchant: Can create templates for own merchant only

    Request Body:
        {
            "merchant": "shop_123",
            "template_name": "order-confirmation",
            "identifier": "shop_123",
            "is_active": true,
            "description": "Order confirmation flow",
            "flow": {
                "initial_node": "greeting",
                "nodes": [
                    {
                        "node_name": "greeting",
                        "task_messages": [...],
                        "functions": [...]
                    }
                ]
            },
            "expected_payload_schema": {...},
            "expected_callback_response_schema": {...}
        }

    Returns:
        {
            "status": "success",
            "template_id": "uuid",
            "message": "Template 'order-confirmation' created successfully with 5 nodes"
        }
    """
    # RBAC: Check permission to create template for this merchant
    require_admin_or_merchant_owner(
        current_user, template_data.merchant, operation="create templates"
    )

    return await create_template_handler(template_data, current_user)


@router.get("/templates")
async def get_templates(
    merchant_id: str = Query(..., description="Merchant ID to filter by"),
    shop_identifier: Optional[str] = Query(
        None, description="Shop identifier to filter by"
    ),
    name: Optional[str] = Query(None, description="Template name to filter by"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get templates filtered by merchant, shop, and/or name.

    Query Parameters:
    - merchant_id: Merchant ID (required)
    - shop_identifier: Shop identifier (optional)
    - name: Template name (optional)

    RBAC:
    - Admin: Can access any merchant's templates
    - Merchant: Can only access own merchant's templates

    Example Requests:
        GET /templates?merchant_id=shop_123                                    # All templates for merchant
        GET /templates?merchant_id=shop_123&name=order-confirmation            # Specific template
        GET /templates?merchant_id=shop_123&shop_identifier=shop_456           # Shop-specific templates

    Returns:
        Template object(s) or empty list if not found
    """
    # RBAC: Check access to merchant and shop
    validate_template_access(
        current_user, merchant_id, shop_identifier, operation="access templates for"
    )

    return await get_template_handler(merchant_id, shop_identifier, name, current_user)
