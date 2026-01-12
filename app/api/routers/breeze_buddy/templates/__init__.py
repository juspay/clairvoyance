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

from app.ai.voice.agents.breeze_buddy.template.types import (
    CreateTemplateRequest,
    TemplateModel,
    UpdateTemplateRequest,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import TemplateListResponse

from .handlers import (
    create_template_handler,
    get_template_by_id_handler,
    list_templates_handler,
    update_template_handler,
)
from .rbac import require_admin_or_merchant_owner

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


@router.get("/templates/list", response_model=TemplateListResponse)
async def list_templates(
    merchant_id: Optional[str] = Query(None, description="Filter by merchant ID"),
    shop_identifier: Optional[str] = Query(
        None, description="Filter by shop identifier"
    ),
    include_inactive: bool = Query(
        False, description="Include inactive templates (default: false)"
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List all accessible templates (metadata only, no flow).

    Returns template metadata without the flow field for optimal performance.
    Automatically filters based on user's RBAC permissions.

    Query Parameters:
    - merchant_id: Optional filter by specific merchant ID
    - shop_identifier: Optional filter by specific shop identifier
    - include_inactive: Include inactive templates (default: false)

    RBAC Behavior:
    - Admin: Returns all templates (optionally filtered by query params)
    - Reseller: Returns templates for all assigned merchants
    - Merchant: Returns templates for assigned merchant(s)
    - Shop: Returns templates for assigned shop(s)

    If no filters specified, returns all templates based on JWT permissions.
    By default, only active templates are returned.

    Performance:
    - ~98.5% smaller response size compared to full templates with flow
    - Optimized for listing/browsing use cases

    Example Requests:
        GET /templates/list                                    # All accessible templates
        GET /templates/list?merchant_id=shop_123              # Templates for specific merchant
        GET /templates/list?include_inactive=true             # Include inactive templates

    Returns:
        TemplateListResponse with templates array and total count
    """
    return await list_templates_handler(
        merchant_id, shop_identifier, include_inactive, current_user
    )


@router.get("/templates/{template_id}", response_model=TemplateModel)
async def get_template_by_id(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Get complete template by ID (includes full flow).

    Returns the complete template object including the flow structure.
    Use this endpoint when you need the template flow for editing or execution.

    Path Parameters:
    - template_id: Template UUID

    RBAC:
    - User must have access to the template's merchant and shop
    - Returns 403 if unauthorized

    Example Request:
        GET /templates/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a

    Returns:
        Complete TemplateModel including flow, schemas, and metadata
    """
    return await get_template_by_id_handler(template_id, current_user)


@router.patch("/templates/{template_id}", status_code=status.HTTP_200_OK)
async def update_template(
    template_id: str,
    update_data: UpdateTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Update a template with partial updates (PATCH).

    Allows updating specific fields of a template without providing the entire template object.
    All fields in the request body are optional - only provided fields will be updated.

    Path Parameters:
    - template_id: Template UUID

    Request Body (all fields optional):
        {
            "template_name": "new-name",           # Optional: Update template name
            "identifier": "new-shop-id",            # Optional: Update shop identifier
            "outbound_number_id": "uuid",           # Optional: Update outbound number
            "is_active": true,                      # Optional: Update active status
            "flow": {...},                          # Optional: Update flow structure
            "expected_payload_schema": {...},       # Optional: Update payload schema
            "expected_callback_response_schema": {...},  # Optional: Update callback schema
            "configurations": {                     # Optional: Update configurations
                "tts_voice_name": "rhea",
                "stt_language": "en-US",
                "payload_based_language_selection": false
            }
        }

    Permissions:
    - Admin: Can update templates for any merchant
    - Merchant: Can update templates for own merchant only
    - User must have access to the template's merchant and shop

    RBAC:
    - Returns 403 if unauthorized
    - Returns 404 if template not found
    - Returns 400 if validation fails or no fields provided

    Example Requests:
        PATCH /templates/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a
        Body: {"is_active": false}                # Disable template

        PATCH /templates/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a
        Body: {"template_name": "updated-name"}   # Rename template

        PATCH /templates/d4e5f6a7-b8c9-0d1e-2f3a-4b5c6d7e8f9a
        Body: {"flow": {...}, "configurations": {...}}  # Update flow and configs

    Returns:
        {
            "status": "success",
            "template_id": "uuid",
            "message": "Template updated successfully with N field(s)",
            "updated_fields": ["field1", "field2", ...]
        }
    """
    return await update_template_handler(template_id, update_data, current_user)
