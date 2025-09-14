from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    WebSocket,
    BackgroundTasks,
    Request,
)
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect
from uuid import uuid4
from datetime import datetime, timedelta, timezone

from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app.schemas import (
    TokenData,
    RequestedBy,
    Workflow,
    CreateOutboundNumberRequest,
    CreateCallExecutionConfigRequest,
)
from app.agents.voice.breeze_buddy.workflows.order_confirmation.types import (
    BreezeOrderData,
)
import aiohttp
from app.agents.voice.breeze_buddy.managers.calls import (
    process_backlog_leads,
    handle_call_completion,
    handle_unanswered_calls,
    update_call_recording,
)
from app.agents.voice.breeze_buddy.services.telephony.utils import get_voice_provider
from app.database.accessor import (
    create_outbound_number,
    get_outbound_number_by_id,
    get_all_outbound_numbers,
    disable_outbound_number,
    get_call_execution_config_by_merchant_id,
    create_call_execution_config,
    create_lead_call_tracker,
)

router = APIRouter()


@router.get("/{provider}/callback/details")
async def callback_status(request: Request, provider: str):
    query_params = dict(request.query_params)
    logger.info(f"Received call-details with {provider} query params: {query_params}")

    if provider.lower() != "exotel":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    recording_url = query_params.get("Stream[RecordingUrl]")
    call_sid = query_params.get("CallSid")

    if recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {recording_url} and call_sid: {call_sid}"
        )
        await update_call_recording(call_sid, recording_url)

    return Response(status_code=200)


@router.post("/{provider}/callback/status")
async def callback_status(request: Request, provider: str):
    """
    Logs the request body and returns a 200 OK response.
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    call_sid = form.get("CallSid")
    call_status = None

    if provider.lower() == "twilio":
        call_status = form.get("CallStatus")
    elif provider.lower() == "exotel":
        call_status = form.get("Status")

    logger.info(
        f"Extracted call_sid: {call_sid} and call_status: {call_status} from {provider}"
    )

    if call_status in ("no-answer", "failed", "busy"):
        logger.info(f"Call with SID {call_sid} failed with status: {call_status}")
        await handle_unanswered_calls(call_sid)

    return Response(status_code=200)


@router.post("/outbound-number")
async def add_outbound_number(
    number: CreateOutboundNumberRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Adds a new outbound number to the database.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} adding new outbound number: {number.number}"
    )

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
            logger.info(
                f"Outbound number {number.number} added successfully with ID {outbound_number.id}"
            )
            return outbound_number
        else:
            logger.error(f"Failed to add outbound number {number.number}")
            raise HTTPException(status_code=400, detail="Failed to add outbound number")

    except Exception as e:
        logger.error(f"Error adding outbound number: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/outbound-number")
async def get_outbound_number(
    id: str = None, current_user: TokenData = Depends(get_current_user)
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
    number_id: str, current_user: TokenData = Depends(get_current_user)
):
    """
    Disables an outbound number in the database.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} disabling outbound number: {number_id}"
    )

    try:
        outbound_number = await disable_outbound_number(number_id)

        if outbound_number:
            logger.info(f"Outbound number {number_id} disabled successfully")
            return outbound_number
        else:
            logger.error(f"Failed to disable outbound number {number_id}")
            raise HTTPException(
                status_code=400, detail="Failed to disable outbound number"
            )

    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{identity}/{workflow}")
async def trigger_order_confirmation(
    identity: RequestedBy,
    workflow: Workflow,
    order: BreezeOrderData,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Receives order details and triggers a order confirmation workflow.
    Requires JWT authentication.
    """
    if identity != "breeze":
        raise HTTPException(status_code=404, detail="Feature not supported")

    logger.info(
        f"Authenticated user {current_user.user_id} requesting order confirmation for order: {order.order_id} for {order.customer_name}"
    )

    try:
        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            identity.value
        )
        if not call_execution_configs:
            raise HTTPException(
                status_code=404,
                detail="Call execution config not found for this merchant",
            )

        config = next(
            (c for c in call_execution_configs if c.workflow == workflow), None
        )
        if not config:
            raise HTTPException(
                status_code=404,
                detail=f"Call execution config not found for workflow: {workflow}",
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
            "identity": identity,
            "reporting_webhook_url": order.reporting_webhook_url,
        }

        # Calculate next attempt time
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.initial_offset
        )

        # Insert lead call tracker record
        lead_call_tracker = await create_lead_call_tracker(
            id=uuid,
            merchant_id=identity,
            workflow=workflow,
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
            raise HTTPException(
                status_code=400, detail="Failed to add lead call tracker to queue"
            )
    except Exception as e:
        logger.error("Error processing order confirmation request", exc_info=True)
        raise HTTPException(status_code=400, detail="Unexpected error") from e


@router.websocket("/{service_provider}/callback/{workflow}")
async def telephony_websocket_handler(
    service_provider: str, workflow: str, websocket: WebSocket
):
    """
    WebSocket endpoint that accepts a connection and passes it to the
    pipecat bot's main function.
    """
    if workflow != "order-confirmation":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service or workflow"
        )

    logger.info(f"Handling websocket for {workflow}")

    async with aiohttp.ClientSession() as session:
        try:
            provider = get_voice_provider(service_provider.upper(), session)
            provider.set_completion_callback(handle_call_completion)
            await provider.handle_websocket(websocket, service_provider.upper())
        except WebSocketDisconnect:
            logger.warning("WebSocket client disconnected.")
        except Exception as e:
            error_type = type(e).__name__
            error_message = str(e)
            logger.error(
                f"An error occurred in the WebSocket handler - Type: {error_type}, Message: '{error_message}', Args: {e.args}",
                exc_info=True,
            )
            try:
                if websocket.client_state.name != "DISCONNECTED":
                    await websocket.close(code=1011, reason="Internal Server Error")
            except Exception as close_error:
                logger.warning(
                    f"Could not close websocket (likely already closed): {close_error}"
                )
        finally:
            logger.info("WebSocket client connection closed.")


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
            workflow=config.workflow,
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
            raise HTTPException(
                status_code=400, detail="Failed to add call execution config"
            )

    except Exception as e:
        logger.error("Error disabling outbound number", exc_info=True)
        raise HTTPException(status_code=400, detail="Unexpected error") from e


@router.get("/cron/initiate")
async def initiate_cron(
    background_tasks: BackgroundTasks,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Initiates the cron job to process backlog leads.
    """
    logger.info(f"Authenticated user {current_user.user_id} initiating cron job")
    background_tasks.add_task(process_backlog_leads)
    return {"status": "success", "message": "Lead processing initiated"}


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
            raise HTTPException(
                status_code=404, detail="Call execution config not found"
            )

    except Exception as e:
        logger.error("Error getting call execution config", exc_info=True)
        raise HTTPException(status_code=400, detail="Unexpected error") from e
