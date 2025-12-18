from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import JSONResponse

from app.core.logger import logger
from app.core.security.jwt import get_breeze_buddy_session, get_current_user
from app.database.accessor import (
    create_call_execution_config,
    get_all_call_execution_configs,
    get_call_execution_config_by_merchant_id,
    update_call_execution_config,
)
from app.schemas import (
    CreateCallExecutionConfigRequest,
    TokenData,
    UpdateCallExecutionConfigRequest,
)

router = APIRouter()


@router.post("/call-execution-config")
async def add_call_execution_config(
    config: CreateCallExecutionConfigRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Adds a new call execution config to the database.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} adding new call execution config for merchant: {config.merchant_id}"
    )

    try:
        call_execution_config = await create_call_execution_config(
            id=str(uuid4()),
            initial_offset=config.initial_offset,
            retry_offset=config.retry_offset,
            call_start_time=config.call_start_time,
            call_end_time=config.call_end_time,
            max_retry=config.max_retry,
            calling_provider=config.calling_provider,
            merchant_id=config.merchant_id,
            template=config.template,
            shop_identifier=config.shop_identifier,
            enable_international_call=config.enable_international_call,
        )

        if call_execution_config:
            logger.info(
                f"Call execution config for merchant {config.merchant_id} added successfully with ID {call_execution_config.id}"
            )
            return call_execution_config
        else:
            logger.error(
                f"Failed to add call execution config for merchant {config.merchant_id}"
            )
            return JSONResponse(
                status_code=400,
                content={"detail": "Failed to add call execution config"},
            )

    except Exception as e:
        logger.error("Error adding call execution config", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error adding call execution config: {str(e)}"},
        )


@router.put("/call-execution-config")
async def update_call_execution_config_endpoint(
    config: UpdateCallExecutionConfigRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Updates an existing call execution config in the database based on merchant_id, template, and shop_identifier.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} updating call execution config for merchant: {config.merchant_id}, template: {config.template}, shop_identifier: {config.shop_identifier}"
    )

    try:
        call_execution_config = await update_call_execution_config(
            merchant_id=config.merchant_id,
            template=config.template,
            shop_identifier=config.shop_identifier,
            initial_offset=config.initial_offset,
            retry_offset=config.retry_offset,
            call_start_time=config.call_start_time,
            call_end_time=config.call_end_time,
            max_retry=config.max_retry,
            calling_provider=config.calling_provider,
            enable_international_call=config.enable_international_call,
        )

        if call_execution_config:
            logger.info(
                f"Call execution config updated successfully for merchant: {config.merchant_id}, template: {config.template}"
            )
            return call_execution_config
        else:
            logger.error(
                f"Failed to update call execution config for merchant: {config.merchant_id}, template: {config.template}, shop_identifier: {config.shop_identifier}"
            )
            return JSONResponse(
                status_code=404, content={"detail": "Call execution config not found"}
            )

    except Exception as e:
        logger.error("Error updating call execution config", exc_info=True)
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error updating call execution config: {str(e)}"},
        )


@router.get("/call-execution-config/{merchant_id}")
async def get_call_execution_config(
    merchant_id: str, current_user: TokenData = Depends(get_current_user)
):
    """
    Gets a call execution config from the database based on the provided merchant ID.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} requesting call execution config for merchant: {merchant_id}"
    )

    try:
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            merchant_id
        )
        if call_execution_configs:
            return call_execution_configs
        else:
            logger.info(f"No call execution config found for merchant: {merchant_id}")
            return []

    except Exception as e:
        logger.error("Error getting call execution config", exc_info=True)
        return JSONResponse(
            status_code=400, content={"detail": f"Unexpected error: {str(e)}"}
        )


@router.get(
    "/breeze/order-confirmation/call-execution-configs",
    include_in_schema=False,
)
async def get_call_execution_configs_for_dashboard(
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides all call execution configs for the dashboard.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_all_call_execution_configs()
