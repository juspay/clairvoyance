from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import JSONResponse

from app.ai.voice.agents.breeze_buddy.template.types import CreateTemplateRequest
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.database.accessor import get_template_by_merchant
from app.database.accessor.breeze_buddy.template import create_template
from app.schemas import TokenData

router = APIRouter()


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
            logger.info(f"Template found: {template.id}")
            return template
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
        )

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Template already exists for this merchant {template_data.merchant} and template name: {template_data.template_name}",
            )

        # Create the template with flow stored as JSON
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
