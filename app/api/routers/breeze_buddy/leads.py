from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from app.ai.voice.agents.breeze_buddy.types.models import LeadData, PushLeadRequest
from app.ai.voice.agents.breeze_buddy.utils.common import (
    get_validation_error_message,
    validate_payload,
)
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_lead_by_id,
    get_template_by_merchant,
)
from app.schemas import TokenData

router = APIRouter()


@router.get("/lead/{lead_id}")
async def get_lead(lead_id: str, current_user: TokenData = Depends(get_current_user)):
    """
    Gets a lead by ID from the database (excluding metadata, cost, is_locked, created_at, updated_at, outbound_number_id).
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} requesting lead: {lead_id}")

    try:
        lead = await get_lead_by_id(lead_id)
        if lead:
            lead_dict = lead.model_dump()
            lead_dict.pop("metaData", None)
            lead_dict.pop("cost", None)
            lead_dict.pop("is_locked", None)
            lead_dict.pop("created_at", None)
            lead_dict.pop("updated_at", None)
            lead_dict.pop("outbound_number_id", None)
            return lead_dict
        else:
            logger.info(f"No lead found for ID: {lead_id}")
            return JSONResponse(
                status_code=404,
                content={"detail": f"Lead not found for ID: {lead_id}"},
            )

    except Exception as e:
        logger.error("Error getting lead", exc_info=True)
        return JSONResponse(
            status_code=400, content={"detail": f"Unexpected error: {str(e)}"}
        )


@router.post("/{merchant}/{template}")
async def trigger_order_confirmation(
    merchant: str,
    template: str,
    order: LeadData,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Receives order details and triggers a order confirmation template.
    Requires JWT authentication.
    """

    logger.info(
        f"Authenticated user {current_user.user_id} requesting order confirmation for order: {order.order_id} for {order.customer_name}"
    )

    try:
        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            merchant, order.shop_identifier
        )
        if not call_execution_configs:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for merchant_id: {merchant}"
                },
            )

        config = next(
            (c for c in call_execution_configs if c.template == template), None
        )
        if not config:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for template: {template}"
                },
            )

        uuid = str(uuid4())
        call_payload = {
            "order_id": order.order_id,
            "customer_name": order.customer_name,
            "shop_name": order.shop_name,
            "total_price": order.total_price,
            "customer_address": order.customer_address,
            "customer_mobile_number": order.customer_mobile_number,
            "order_data": order.order_data.model_dump(),
            "reporting_webhook_url": order.reporting_webhook_url,
        }

        # Calculate next attempt time
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.initial_offset
        )

        # Insert lead call tracker record
        lead_call_tracker = await create_lead_call_tracker(
            id=uuid,
            merchant_id=merchant,
            template=template,
            shop_identifier=order.shop_identifier,
            next_attempt_at=next_attempt_at,
            payload=call_payload,
            attempt_count=0,
        )

        if lead_call_tracker:
            logger.info(
                f"Lead call tracker {order.order_id} added to queue with ID {uuid}"
            )

            return {
                "status": "queued",
                "lead_call_tracker_id": uuid,
                "order_id": order.order_id,
                "message": "Call request added to queue for processing",
            }
        else:
            logger.error(f"Failed to add lead call tracker {order.order_id} to queue")
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Failed to add lead call tracker for order_id: {order.order_id}"
                },
            )
    except Exception as e:
        logger.error("Error processing order confirmation request", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error processing order confirmation: {str(e)}"},
        )


@router.post("/push/lead/v2")
async def push_lead_v2(
    req: PushLeadRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Receives lead details and adds in database for processing.
    Requires JWT authentication.
    """

    logger.info(
        f"Authenticated user {current_user.user_id} requesting order confirmation for order"
    )

    try:
        # Fetch template to get expected payload schema
        template = await get_template_by_merchant(
            req.merchant, req.identifier, req.template
        )

        if not template:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Template '{req.template}' not found for merchant: {req.merchant}"
                },
            )

        # Validate payload against expected schema if schema exists
        if template.expected_payload_schema:
            is_valid, validation_errors = validate_payload(
                req.payload, template.expected_payload_schema
            )

            if not is_valid:
                error_message = get_validation_error_message(validation_errors)
                logger.warning(
                    f"Payload validation failed for merchant {req.merchant}: {error_message}"
                )
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": error_message,
                        "errors": validation_errors,
                    },
                )

            logger.info(f"Payload validation successful for merchant {req.merchant}")

        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            req.merchant, req.identifier
        )
        if not call_execution_configs:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for merchant_id: {req.merchant}"
                },
            )

        config = next(
            (c for c in call_execution_configs if c.template == req.template), None
        )
        if not config:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for template: {req.template}"
                },
            )

        uuid = str(uuid4())

        # Calculate next attempt time
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.initial_offset
        )

        # Prepare payload with reporting webhook URL
        lead_payload = {**req.payload}
        if req.reporting_webhook_url:
            lead_payload["reporting_webhook_url"] = req.reporting_webhook_url

        # Insert lead call tracker record
        lead_call_tracker = await create_lead_call_tracker(
            id=uuid,
            merchant_id=req.merchant,
            template=req.template,
            shop_identifier=req.identifier,
            next_attempt_at=next_attempt_at,
            payload=lead_payload,
            attempt_count=0,
            meta_data={"use_template_flow": True},
        )

        if lead_call_tracker:
            logger.info(
                f"Lead call tracker {req.payload.get('order_id')} added to queue with ID {uuid}"
            )

            return {
                "status": "queued",
                "lead_call_tracker_id": uuid,
                "order_id": req.payload.get("order_id"),
                "message": "Call request added to queue for processing",
            }
        else:
            logger.error(
                f"Failed to add lead call tracker {req.payload.get('order_id')} to queue"
            )
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Failed to add lead call tracker for order_id: {req.payload.get('order_id')}"
                },
            )
    except Exception as e:
        logger.error("Error processing order confirmation request", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error processing order confirmation: {str(e)}"},
        )
