"""
Business logic handlers for template operations.
All handlers perform database operations and enforce business rules.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.template.types import CreateTemplateRequest
from app.core.logger import logger
from app.database.accessor import get_outbound_number_by_id, get_template_by_merchant
from app.database.accessor.breeze_buddy.template import (
    create_template,
    get_template_by_id,
    get_templates_list,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template import TemplateListResponse

from .rbac import apply_hierarchical_template_filters, validate_template_access


async def create_template_handler(
    template_data: CreateTemplateRequest, current_user: UserInfo
):
    """
    Create a new template.

    Args:
        template_data: Template creation request
        current_user: Current authenticated user

    Returns:
        Success response with template ID

    Raises:
        HTTPException: 409 if template exists, 400/500 on error
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) creating template "
        f"for merchant: {template_data.merchant}, name: {template_data.template_name}"
    )

    try:
        # Validate flow structure
        flow = template_data.flow
        if not flow:
            raise ValueError("Flow structure is required")

        if "initial_node" not in flow:
            raise ValueError("initial_node must be specified in flow structure")

        if "nodes" not in flow or not flow["nodes"]:
            raise ValueError("nodes must be specified in flow structure")

        # Check if template already exists
        existing = await get_template_by_merchant(
            template_data.merchant,
            template_data.identifier,
            template_data.template_name,
            should_prioritize_merchant_specific=False,
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template already exists for merchant {template_data.merchant} "
                f"and template name: {template_data.template_name}",
            )

        # Validate outbound_number_id if provided
        if template_data.outbound_number_id:
            outbound_number = await get_outbound_number_by_id(
                template_data.outbound_number_id
            )
            if not outbound_number:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Outbound number with ID {template_data.outbound_number_id} does not exist",
                )

        # Create the template
        now = datetime.now(timezone.utc)

        # Build configurations dict from the ConfigurationModel
        configurations = None
        if template_data.configurations:
            configurations = template_data.configurations.model_dump(exclude_none=True)

        template = await create_template(
            template_id=str(uuid4()),
            merchant=template_data.merchant,
            identifier=template_data.identifier,
            name=template_data.template_name,
            flow=flow,
            expected_payload_schema=template_data.expected_payload_schema,
            expected_callback_response_schema=template_data.expected_callback_response_schema,
            configurations=configurations,
            outbound_number_id=template_data.outbound_number_id,
            is_active=template_data.is_active,
            now=now,
        )

        if not template:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to create template",
            )

        logger.info(
            f"Successfully created template with id: {template.id} containing flow "
            f"with {len(flow.get('nodes', []))} nodes"
        )

        return {
            "status": "success",
            "template_id": template.id,
            "message": f"Template '{template_data.template_name}' created successfully "
            f"with {len(flow.get('nodes', []))} nodes",
        }

    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error creating template: {e}")
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error creating template: {str(e)}",
        )


async def get_template_handler(
    merchant_id: str,
    shop_identifier: Optional[str],
    name: Optional[str],
    current_user: UserInfo,
):
    """
    Get template(s) by merchant, shop, and name.

    Args:
        merchant_id: Merchant ID
        shop_identifier: Optional shop identifier
        name: Optional template name
        current_user: Current authenticated user

    Returns:
        Template object or list of templates
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting template "
        f"for merchant: {merchant_id}, shop: {shop_identifier}, name: {name}"
    )

    try:
        template = await get_template_by_merchant(
            merchant_id=merchant_id,
            shop_identifier=shop_identifier,
            name=name,
        )

        if template:
            logger.info(f"Template found: {template.id}")
            return template
        else:
            logger.info(
                f"No template found for merchant: {merchant_id}, "
                f"shop_identifier: {shop_identifier}, name: {name}"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template '{name}' not found for merchant: {merchant_id}",
            )

    except Exception as e:
        logger.error(f"Error getting template: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting template: {str(e)}",
        )


async def list_templates_handler(
    merchant_id: Optional[str],
    shop_identifier: Optional[str],
    include_inactive: bool,
    current_user: UserInfo,
) -> TemplateListResponse:
    """
    List templates with RBAC enforcement.

    Returns metadata only (no flow) for optimal performance.

    Args:
        merchant_id: Optional merchant ID to filter by
        shop_identifier: Optional shop identifier to filter by
        include_inactive: Whether to include inactive templates
        current_user: Current authenticated user

    Returns:
        TemplateListResponse with list of template metadata

    Raises:
        HTTPException: 403 if user tries to access unauthorized merchants/shops
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting templates list: "
        f"merchant_id={merchant_id}, shop_identifier={shop_identifier}, include_inactive={include_inactive}"
    )

    try:
        # Build filters from query params
        filters = {}
        if merchant_id:
            filters["merchant_id"] = merchant_id
        if shop_identifier:
            filters["shop_identifier"] = shop_identifier
        if not include_inactive:
            filters["is_active"] = True

        # Apply RBAC filtering (validates access and injects user's accessible merchants/shops)
        filters = apply_hierarchical_template_filters(filters, current_user)

        # Get templates from database
        templates = await get_templates_list(filters)

        logger.info(
            f"Returning {len(templates)} templates for user {current_user.username}"
        )

        return TemplateListResponse(templates=templates, total=len(templates))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing templates: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error listing templates: {str(e)}",
        )


async def get_template_by_id_handler(template_id: str, current_user: UserInfo):
    """
    Get complete template by ID with RBAC validation.

    Returns full template including flow structure.

    Args:
        template_id: Template UUID
        current_user: Current authenticated user

    Returns:
        Complete TemplateModel with flow

    Raises:
        HTTPException: 404 if template not found, 403 if access denied
    """
    logger.info(
        f"User {current_user.username} (role: {current_user.role}) requesting template by ID: {template_id}"
    )

    try:
        # Get template by ID
        template = await get_template_by_id(template_id)

        if not template:
            logger.warning(f"Template not found: {template_id}")
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Template not found: {template_id}",
            )

        # Validate RBAC access
        validate_template_access(
            current_user,
            template.merchant_id,
            template.shop_identifier,
            operation="access template",
        )

        logger.info(f"Returning template {template_id} to user {current_user.username}")

        return template

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting template by ID: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error getting template: {str(e)}",
        )
