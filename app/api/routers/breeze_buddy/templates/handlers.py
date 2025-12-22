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
from app.database.accessor import get_template_by_merchant
from app.database.accessor.breeze_buddy.template import create_template
from app.schemas import UserInfo


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
        )

        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Template already exists for merchant {template_data.merchant} "
                f"and template name: {template_data.template_name}",
            )

        # Create the template
        now = datetime.now(timezone.utc)
        template = await create_template(
            template_id=str(uuid4()),
            merchant=template_data.merchant,
            identifier=template_data.identifier,
            name=template_data.template_name,
            flow=flow,
            expected_payload_schema=template_data.expected_payload_schema,
            expected_callback_response_schema=template_data.expected_callback_response_schema,
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
