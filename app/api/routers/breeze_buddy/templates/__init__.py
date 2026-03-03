"""
Modern RESTful template management endpoints with RBAC.

This module provides clean REST API endpoints for managing call flow templates.
Templates define the conversational flow for automated calls.

Endpoints:
- POST   /templates           - Create new template
- GET    /templates           - Get templates (filtered by merchant/shop/name)
- PUT    /templates/{id}      - Replace existing template by ID
- DELETE /templates/{id}      - Delete template by ID (admin only)

For backward compatibility, old endpoints are available in deprecated/template.py
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, status

from app.ai.voice.agents.breeze_buddy.template.types import (
    CreateTemplateRequest,
    ReplaceTemplateRequest,
    TemplateModel,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import (
    DeleteTemplateResponse,
    TemplateListResponse,
)

from .handlers import (
    create_template_handler,
    delete_template_handler,
    get_template_by_id_handler,
    list_templates_handler,
    replace_template_handler,
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
            "merchant_id": "shop_123",
            "name": "order-confirmation",
            "shop_identifier": "shop_123",
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
        current_user, template_data.merchant_id, operation="create templates"
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


@router.put("/templates/{template_id}", response_model=TemplateModel)
async def replace_template(
    template_id: str,
    template_data: ReplaceTemplateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    replace an existing template.

    replaces all fields of the template. Non-nullable fields (name, flow, is_active)
    must be provided. Nullable fields (shop_identifier, outbound_number_id,
    expected_payload_schema, expected_callback_response_schema, configurations)
    - if not provided, they will be set to NULL.

    Path Parameters:
    - template_id: Template UUID

    Request Body:
        {
            "name": "updated-template-name",
            "shop_identifier": "shop_identifier",
            "outbound_number_id": "uuid",
            "is_active": true,
            "flow": {...},
            "expected_payload_schema": {...},
            "expected_callback_response_schema": {...},
            "configurations": {...}
        }

    Returns:
        Updated TemplateModel including flow, schemas, and metadata

    Raises:
        - 404: Template not found
        - 400: Validation error (missing required fields)
    """
    return await replace_template_handler(template_id, template_data, current_user)


@router.delete("/templates/{template_id}", response_model=DeleteTemplateResponse)
async def delete_template_by_id(
    template_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Delete a template by ID (admin only).

    Performs safety checks before deletion to ensure the template is not in use:
    - Checks if any call_execution_config references this template
    - Checks if any active leads (BACKLOG/RETRY/PROCESSING) are using this template

    If the template is in use, returns 409 Conflict with details about what is
    referencing it. The references must be removed before the template can be deleted.

    Path Parameters:
    - template_id: Template UUID

    Permissions:
    - Admin only

    Returns:
        DeleteTemplateResponse with deleted template metadata

    Raises:
        - 403: Non-admin user
        - 404: Template not found
        - 409: Template is in use and cannot be safely deleted
    """
    require_admin(current_user)

    return await delete_template_handler(template_id, current_user)
