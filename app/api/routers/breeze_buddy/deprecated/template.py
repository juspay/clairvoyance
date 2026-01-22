from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import JSONResponse

from app.ai.voice.agents.breeze_buddy.template.types import (
    CreateTemplateRequest,
    TemplateModel,
)
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.database.accessor import get_outbound_number_by_id, get_template_by_merchant
from app.database.accessor.breeze_buddy.template import create_template
from app.schemas import TokenData

router = APIRouter()

# Mask value for secrets in API responses
SECRETS_MASK = "****"


def mask_secrets(secrets: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Mask all secret values with **** for API responses."""
    if not secrets:
        return None
    return {key: SECRETS_MASK for key in secrets.keys()}


def mask_template_secrets(template: TemplateModel) -> TemplateModel:
    """Return a copy of the template with secrets masked for API responses."""
    return TemplateModel(
        id=template.id,
        merchant_id=template.merchant_id,
        shop_identifier=template.shop_identifier,
        name=template.name,
        flow=template.flow,
        expected_payload_schema=template.expected_payload_schema,
        expected_callback_response_schema=template.expected_callback_response_schema,
        configurations=template.configurations,
        secrets=mask_secrets(template.secrets),
        outbound_number_id=template.outbound_number_id,
        is_active=template.is_active,
        rendered_system_prompt=template.rendered_system_prompt,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


@router.get("/template")
async def get_template(
    merchant_id: str,
    shop_identifier: str = None,
    name: str = None,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Gets a template by merchant ID, shop identifier, and name.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} requesting template for merchant: {merchant_id}"
    )

    try:
        template = await get_template_by_merchant(
            merchant_id=merchant_id,
            shop_identifier=shop_identifier,
            name=name,
        )

        if template:
            logger.info(f"Template found: {template}")
            return mask_template_secrets(template)
        else:
            logger.info(
                f"No template found for merchant: {merchant_id}, shop_identifier: {shop_identifier}, name: {name}"
            )
            return []

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting template: {e}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error getting template: {str(e)}")


@router.post("/template")
async def create_template_from_json(
    template_data: CreateTemplateRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Creates a complete template from a JSON object using the new simplified architecture.
    Requires JWT authentication.
    """
    logger.info(f"User {current_user.user_id} is creating/updating a template.")

    try:
        # Validate that flow structure contains required fields
        flow = template_data.flow
        if not flow:
            raise ValueError("Flow structure is required")

        if "initial_node" not in flow:
            raise ValueError("initial_node must be specified in flow structure")

        if "nodes" not in flow or not flow["nodes"]:
            raise ValueError("nodes must be specified in flow structure")

        existing = await get_template_by_merchant(
            template_data.merchant,
            template_data.identifier,
            template_data.template_name,
            should_prioritize_merchant_specific=False,
        )

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Template already exists for this merchant {template_data.merchant} and template name: {template_data.template_name}",
            )

        # Validate outbound_number_id if provided
        if template_data.outbound_number_id:
            outbound_number = await get_outbound_number_by_id(
                template_data.outbound_number_id
            )
            if not outbound_number:
                raise HTTPException(
                    status_code=400,
                    detail=f"Outbound number with ID {template_data.outbound_number_id} does not exist",
                )

        # Create the template with flow stored as JSON
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
            secrets=template_data.secrets,
            outbound_number_id=template_data.outbound_number_id,
            is_active=template_data.is_active,
            now=now,
        )

        if not template:
            raise HTTPException(status_code=500, detail="Failed to create template")

        logger.info(
            f"Successfully created template with id: {template.id} containing flow with {len(flow.get('nodes', []))} nodes"
        )

        return JSONResponse(
            status_code=201,
            content={
                "status": "success",
                "template_id": template.id,
                "message": f"Template '{template_data.template_name}' created successfully with {len(flow.get('nodes', []))} nodes",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating template from JSON: {e}", exc_info=True)
        return JSONResponse(status_code=400, content={"detail": str(e)})
