from fastapi import APIRouter, Depends, HTTPException, WebSocket
from starlette.websockets import WebSocketDisconnect
from uuid import uuid4
from datetime import datetime

from app.core.logger import logger
from app.core.config import BREEZE_BUDDY_CALL_PROVIDER, EXOTEL_FROM_NUMBER, TWILIO_FROM_NUMBER
from app.core.security.jwt import get_current_user
from app.schemas import (
    TokenData,
    CallStatus,
    RequestedBy,
    Workflow,
    CreateOutboundNumberRequest,
    CreateCallExecutionConfigRequest,
)
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.services.call_queue_manager import CallQueueManager
from app.database.accessor.main import (
    create_call_data,
    create_outbound_number,
    get_outbound_number_by_id,
    get_all_outbound_numbers,
    disable_outbound_number,
    get_call_execution_config_by_merchant_id,
    create_call_execution_config,
)

router = APIRouter()

@router.post("/outbound-number")
async def add_outbound_number(
    number: CreateOutboundNumberRequest,
    current_user: TokenData = Depends(get_current_user)
):
    """
    Adds a new outbound number to the database.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} adding new outbound number: {number.number}")

    try:
        outbound_number = await create_outbound_number(
            id=str(uuid4()),
            number=number.number,
            provider=number.provider,
            status=number.status,
            channels=0,
            maximum_channels=number.maximum_channels,
        )
        
        if outbound_number:
            logger.info(f"Outbound number {number.number} added successfully with ID {outbound_number.id}")
            return outbound_number
        else:
            logger.error(f"Failed to add outbound number {number.number}")
            raise HTTPException(status_code=400, detail="Failed to add outbound number")
            
    except Exception as e:
        logger.error(f"Error adding outbound number: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.get("/outbound-number")
async def get_outbound_number(
    id: str = None,
    current_user: TokenData = Depends(get_current_user)
):
    """
    Gets an outbound number from the database based on the provided query parameters.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} requesting outbound number")

    try:
        if id:
            outbound_number = await get_outbound_number_by_id(id)
            if outbound_number:
                return outbound_number
            else:
                raise HTTPException(status_code=404, detail="Outbound number not found")
        else:
            return await get_all_outbound_numbers()
            
    except Exception as e:
        logger.error(f"Error getting outbound number: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/outbound-number/{number_id}")
async def delete_outbound_number(
    number_id: str,
    current_user: TokenData = Depends(get_current_user)
):
    """
    Disables an outbound number in the database.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} disabling outbound number: {number_id}")

    try:
        outbound_number = await disable_outbound_number(number_id)
        
        if outbound_number:
            logger.info(f"Outbound number {number_id} disabled successfully")
            return outbound_number
        else:
            logger.error(f"Failed to disable outbound number {number_id}")
            raise HTTPException(status_code=400, detail="Failed to disable outbound number")
            
    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{identity}/{workflow}")
async def trigger_order_confirmation(
    identity: RequestedBy,
    workflow: Workflow,
    order: BreezeOrderData,
    current_user: TokenData = Depends(get_current_user),
    call_queue_manager: CallQueueManager = Depends(CallQueueManager)
):
    """
    Receives order details and triggers a order confirmation workflow.
    Requires JWT authentication.
    """
    if identity != "breeze":
        raise HTTPException(status_code=404, detail="Feature not supported")

    logger.info(f"Authenticated user {current_user.user_id} requesting order confirmation for order: {order.order_id} for {order.customer_name}")

    try:
        uuid = str(uuid4())
        call_payload = {
            "order_id": order.order_id,
            "customer_name": order.customer_name,
            "shop_name": order.shop_name,
            "total_price": order.total_price,
            "customer_address": order.customer_address,
            "customer_mobile_number": order.customer_mobile_number,
            "order_data": order.order_data.model_dump(),
            "identity": identity,
            "reporting_webhook_url": order.reporting_webhook_url
        }
        
        # Insert call request into database
        call_data = await create_call_data(
            id=uuid,
            outcome=None,
            transcription=None,
            call_start_time=datetime.now().isoformat(),
            call_end_time=None,
            call_id=None,
            provider=BREEZE_BUDDY_CALL_PROVIDER,
            status=CallStatus.BACKLOG,
            requested_by=identity,
            workflow=workflow,
            call_payload=call_payload,
            assigned_number=TWILIO_FROM_NUMBER if BREEZE_BUDDY_CALL_PROVIDER == "twilio" else EXOTEL_FROM_NUMBER,
        )
        
        if call_data:
            logger.info(f"Call request {order.order_id} added to queue with ID {uuid}")
            
            call_queue_manager.trigger_processing()
            
            return {
                "status": "queued",
                "call_data_id": uuid,
                "order_id": order.order_id,
                "message": "Call request added to queue for processing"
            }
        else:
            logger.error(f"Failed to add call request {order.order_id} to queue")
            raise HTTPException(status_code=400, detail="Failed to add call request to queue")
            
    except Exception as e:
        logger.error(f"Error processing order confirmation request: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.websocket("/{service_provider}/callback/{workflow}")
async def telephony_websocket_handler(service_provider: str, workflow: str, websocket: WebSocket, call_queue_manager: CallQueueManager = Depends(CallQueueManager)):
    """
    WebSocket endpoint that accepts a connection and passes it to the
    pipecat bot's main function.
    """
    if workflow != "order-confirmation":
        raise HTTPException(status_code=404, detail="Feature not supported for this service or workflow")
    
    logger.info(f"Handling websocket for {workflow}")
    
    # Get the provider from the call queue manager
    provider = call_queue_manager.voice_provider

    try:
        # The websocket_bot_main function handles the entire
        # lifecycle of the WebSocket connection, including accept().
        await provider.handle_websocket(websocket)
    except WebSocketDisconnect:
        logger.warning("WebSocket client disconnected.")
    except Exception as e:
        error_type = type(e).__name__
        error_message = str(e)
        logger.error(f"An error occurred in the WebSocket handler - Type: {error_type}, Message: '{error_message}', Args: {e.args}", exc_info=True)
        # Only try to close the websocket if it's still open
        try:
            if websocket.client_state.name != "DISCONNECTED":
                await websocket.close(code=1011, reason="Internal Server Error")
        except Exception as close_error:
            logger.warning(f"Could not close websocket (likely already closed): {close_error}")
    finally:
        logger.info("WebSocket client connection closed.")

@router.post("/agent/voice/breeze-buddy/call-execution-config")
async def add_call_execution_config(
    config: CreateCallExecutionConfigRequest,
    current_user: TokenData = Depends(get_current_user)
):
    """
    Adds a new call execution config to the database.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} adding new call execution config for merchant: {config.merchant_id}")

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
            workflow=config.workflow,
        )
        
        if call_execution_config:
            logger.info(f"Call execution config for merchant {config.merchant_id} added successfully with ID {call_execution_config.id}")
            return call_execution_config
        else:
            logger.error(f"Failed to add call execution config for merchant {config.merchant_id}")
            raise HTTPException(status_code=400, detail="Failed to add call execution config")
            
    except Exception as e:
        logger.error("Error disabling outbound number", exc_info=True)
        raise HTTPException(status_code=400, detail="Unexpected error") from e

@router.get("/agent/voice/breeze-buddy/call-execution-config/{merchant_id}")
async def get_call_execution_config(
    merchant_id: str,
    current_user: TokenData = Depends(get_current_user)
):
    """
    Gets a call execution config from the database based on the provided merchant ID.
    Requires JWT authentication.
    """
    logger.info(f"Authenticated user {current_user.user_id} requesting call execution config for merchant: {merchant_id}")

    try:
        call_execution_configs = await get_call_execution_config_by_merchant_id(merchant_id)
        if call_execution_configs:
            return call_execution_configs
        else:
            raise HTTPException(status_code=404, detail="Call execution config not found")
            
    except Exception as e:
        logger.error("Error getting call execution config", exc_info=True)
        raise HTTPException(status_code=400, detail="Unexpected error") from e

