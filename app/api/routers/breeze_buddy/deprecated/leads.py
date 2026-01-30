from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends
from starlette.responses import JSONResponse

from app.ai.voice.agents.breeze_buddy.types.models import LeadData, PushLeadRequest
from app.ai.voice.agents.breeze_buddy.utils.common import (
    get_validation_error_message,
    validate_payload,
)
from app.ai.voice.agents.breeze_buddy.utils.language_utils.language_detector import (
    determine_language_for_call,
)
from app.core.config.dynamic import SHOPS_FOR_TEMPLATE_FLOW
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.database.accessor import (
    create_lead_call_tracker,
    get_call_execution_config_by_merchant_id,
    get_template_by_merchant,
)
from app.schemas import TokenData

router = APIRouter()


@router.post("/{merchant}/{template}")
async def trigger_order_confirmation(
    merchant: str,
    template: str,
    order: LeadData,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Receives order details and triggers a order confirmation template.
    Routes to push_lead_v2 if shop_identifier is in SHOPS_FOR_TEMPLATE_FLOW.
    Requires JWT authentication.
    """

    logger.info(
        f"Authenticated user {current_user.user_id} requesting order confirmation for order: {order.order_id} for {order.customer_name}"
    )

    try:
        # Get dynamic config for shops enabled for template flow
        shops_for_template_flow = await SHOPS_FOR_TEMPLATE_FLOW()

        # Check if shop_identifier is in enabled list for template flow
        if order.shop_identifier and order.shop_identifier in shops_for_template_flow:
            logger.info(
                f"Shop {order.shop_identifier} is in template flow enabled list, routing to push_lead_v2"
            )

            # Fetch template to detect language for agent.py flow
            template_obj = await get_template_by_merchant(
                merchant, order.shop_identifier, template
            )

            # Detect language for agent.py flow (only for template flow enabled shops)
            order_payload = order.model_dump()
            _, language_name = await determine_language_for_call(
                template_obj.configurations if template_obj else None,
                order_payload,
                order.order_id,
            )

            # Transform LeadData to PushLeadRequest format with language
            push_request = PushLeadRequest(
                merchant=merchant,
                template=template,
                identifier=order.shop_identifier,
                reporting_webhook_url=order.reporting_webhook_url,
                request_id=order.order_id,
                payload={
                    "customer_name": order.customer_name,
                    "shop_name": order.shop_name,
                    "total_price": order.total_price,
                    "customer_address": order.customer_address,
                    "customer_mobile_number": order.customer_mobile_number,
                    "items": [item.model_dump() for item in order.order_data.items],
                    "language_name": language_name,
                },
            )

            # Call push_lead_v2 logic
            return await push_lead_v2(push_request, current_user)

        # Fallback to original logic for shops not in the enabled list
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
            request_id=order.order_id,
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
            request_id=req.request_id,
        )

        if lead_call_tracker:
            logger.info(
                f"Lead call tracker {req.request_id} added to queue with ID {uuid}"
            )

            return {
                "status": "queued",
                "lead_call_tracker_id": uuid,
                "order_id": req.request_id,
                "message": "Call request added to queue for processing",
            }
        else:
            logger.error(f"Failed to add lead call tracker {req.request_id} to queue")
            return JSONResponse(
                status_code=400,
                content={
                    "detail": f"Failed to add lead call tracker for request_id: {req.request_id}"
                },
            )
    except Exception as e:
        logger.error("Error processing order confirmation request", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error processing order confirmation: {str(e)}"},
        )
