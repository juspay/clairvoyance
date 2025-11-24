from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4

import jwt
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
from fastapi.responses import RedirectResponse
from starlette.responses import FileResponse, JSONResponse
from starlette.websockets import WebSocketDisconnect

from app.agents.voice.breeze_buddy.managers.calls import (
    handle_call_completion,
    handle_unanswered_calls,
    process_backlog_leads,
    update_call_recording,
)
from app.agents.voice.breeze_buddy.services.telephony.utils import get_voice_provider
from app.agents.voice.breeze_buddy.workflows.order_confirmation.types import (
    BreezeOrderData,
    LoginRequest,
)
from app.core.config import (
    BREEZE_BUDDY_DASHBOARD_PASSWORD,
    BREEZE_BUDDY_DASHBOARD_USERNAME,
    BREEZE_BUDDY_SESSION_SECRET_KEY,
    JWT_ALGORITHM,
)
from app.core.logger import logger
from app.core.security.jwt import get_breeze_buddy_session, get_current_user
from app.core.transport.http_client import create_aiohttp_session
from app.database.accessor import (
    create_call_execution_config,
    create_lead_call_tracker,
    create_outbound_number,
    disable_outbound_number,
    get_all_call_execution_configs,
    get_all_lead_call_trackers,
    get_all_outbound_numbers,
    get_call_execution_config_by_merchant_id,
    get_lead_based_analytics,
    get_lead_by_id,
    get_lead_call_trackers_count,
    get_outbound_number_by_id,
    update_call_execution_config,
)
from app.schemas import (
    CreateCallExecutionConfigRequest,
    CreateOutboundNumberRequest,
    TokenData,
    UpdateCallExecutionConfigRequest,
    Workflow,
)

router = APIRouter()


@router.get("/login", include_in_schema=False)
async def get_login_page():
    return FileResponse(
        "app/agents/voice/breeze_buddy/workflows/order_confirmation/login.html"
    )


@router.post("/login", include_in_schema=False)
async def login(login_request: LoginRequest, response: Response):
    if (
        login_request.username == BREEZE_BUDDY_DASHBOARD_USERNAME
        and login_request.password == BREEZE_BUDDY_DASHBOARD_PASSWORD
    ):
        session_data = {
            "username": login_request.username,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        }
        session_cookie = jwt.encode(
            session_data, BREEZE_BUDDY_SESSION_SECRET_KEY, algorithm=JWT_ALGORITHM
        )
        response.set_cookie(key="session", value=session_cookie, httponly=True)
        return {"message": "Login successful"}
    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/logout", include_in_schema=False)
async def logout():
    response = RedirectResponse(url="/agent/voice/breeze-buddy/login")
    response.delete_cookie("session")
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@router.get("/{provider}/callback/details")
async def callback_details(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    query_params = dict(request.query_params)
    logger.info(f"Received call-details with {provider} query params: {query_params}")

    if provider.lower() != "exotel":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = query_params.get("CallSid")
    provider_recording_url = query_params.get("Stream[RecordingUrl]")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

    return Response(status_code=200)


@router.post("/{provider}/callback/details")
async def callback_details_post(
    request: Request, provider: str, background_tasks: BackgroundTasks
):
    """
    Logs the request body and returns a 200 OK response.
    """
    form = await request.form()
    logger.info(f"Received callback from {provider} with form data: {form}")

    if provider.lower() != "twilio":
        raise HTTPException(
            status_code=404, detail="Feature not supported for this service provider"
        )

    call_sid = form.get("CallSid")
    provider_recording_url = form.get("RecordingUrl")

    if provider_recording_url and call_sid:
        logger.info(
            f"Extracted recording_url: {provider_recording_url} and call_sid: {call_sid}"
        )
        background_tasks.add_task(
            update_call_recording, call_sid, provider_recording_url, provider.lower()
        )

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
            return JSONResponse(
                status_code=400, content={"detail": "Failed to add outbound number"}
            )

    except Exception as e:
        logger.error(f"Error adding outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error adding outbound number: {str(e)}"},
        )


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
                return []
        else:
            return await get_all_outbound_numbers()

    except Exception as e:
        logger.error(f"Error getting outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error getting outbound number: {str(e)}"},
        )


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
            return JSONResponse(
                status_code=400, content={"detail": "Failed to disable outbound number"}
            )

    except Exception as e:
        logger.error(f"Error disabling outbound number: {e}")
        return JSONResponse(
            status_code=400,
            content={"detail": f"Error disabling outbound number: {str(e)}"},
        )


@router.post("/{identity}/{workflow}")
async def trigger_order_confirmation(
    identity: str,
    workflow: Workflow,
    order: BreezeOrderData,
    current_user: TokenData = Depends(get_current_user),
):
    """
    Receives order details and triggers a order confirmation workflow.
    Requires JWT authentication.
    """

    logger.info(
        f"Authenticated user {current_user.user_id} requesting order confirmation for order: {order.order_id} for {order.customer_name}"
    )

    try:
        # Get call execution config
        call_execution_configs = await get_call_execution_config_by_merchant_id(
            identity, order.shop_identifier
        )
        if not call_execution_configs:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for merchant_id: {identity}"
                },
            )

        config = next(
            (c for c in call_execution_configs if c.workflow == workflow), None
        )
        if not config:
            return JSONResponse(
                status_code=404,
                content={
                    "detail": f"Call execution config not found for workflow: {workflow}"
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

    async with create_aiohttp_session() as session:
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
    Updates an existing call execution config in the database based on merchant_id, workflow, and shop_identifier.
    Requires JWT authentication.
    """
    logger.info(
        f"Authenticated user {current_user.user_id} updating call execution config for merchant: {config.merchant_id}, workflow: {config.workflow}, shop_identifier: {config.shop_identifier}"
    )

    try:
        call_execution_config = await update_call_execution_config(
            merchant_id=config.merchant_id,
            workflow=config.workflow,
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
                f"Call execution config updated successfully for merchant: {config.merchant_id}, workflow: {config.workflow}"
            )
            return call_execution_config
        else:
            logger.error(
                f"Failed to update call execution config for merchant: {config.merchant_id}, workflow: {config.workflow}, shop_identifier: {config.shop_identifier}"
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
            logger.info(f"No call execution config found for merchant: {merchant_id}")
            return []

    except Exception as e:
        logger.error("Error getting call execution config", exc_info=True)
        return JSONResponse(
            status_code=400, content={"detail": f"Unexpected error: {str(e)}"}
        )


@router.get("/breeze/order-confirmation/dashboard", include_in_schema=False)
async def get_dashboard(session: dict = Depends(get_breeze_buddy_session)):
    """
    Serves the dashboard HTML file.
    """
    if not session:
        return RedirectResponse(url="/agent/voice/breeze-buddy/login")
    response = FileResponse(
        "app/agents/voice/breeze_buddy/workflows/order_confirmation/dashboard.html"
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@router.get("/breeze/order-confirmation/outbound-numbers", include_in_schema=False)
async def get_outbound_numbers_for_dashboard(
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides all outbound numbers for the dashboard.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return await get_all_outbound_numbers()


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


@router.get("/breeze/order-confirmation/analytics", include_in_schema=False)
async def get_analytics(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides analytics data for the dashboard with both call-based and lead-based metrics.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        start_datetime = (
            datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            if start_date
            else None
        )
        end_datetime = (
            (
                datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            if end_date
            else None
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format. Expected ISO format (YYYY-MM-DD): {str(e)}",
        ) from e

    trackers = await get_all_lead_call_trackers(
        start_date=start_datetime, end_date=end_datetime
    )

    # Call-based analytics (counts every call)
    call_based = {
        "calls_attempted": len(
            [t for t, _ in trackers if t.status and t.status.value == "FINISHED"]
        ),
        "no_answer": len(
            [t for t, _ in trackers if t.outcome and t.outcome.value == "NO_ANSWER"]
        ),
        "connected_and_busy": len(
            [t for t, _ in trackers if t.outcome and t.outcome.value == "BUSY"]
        ),
        "address_confirmed": len(
            [t for t, _ in trackers if t.outcome and t.outcome.value == "CONFIRM"]
        ),
        "order_cancelled": len(
            [t for t, _ in trackers if t.outcome and t.outcome.value == "CANCEL"]
        ),
        "address_updated": len(
            [
                t
                for t, _ in trackers
                if t.outcome and t.outcome.value == "ADDRESS_UPDATED"
            ]
        ),
    }
    # Get all lead details for lead-based analytics

    lead_data = await get_lead_based_analytics(
        start_date=start_datetime, end_date=end_datetime
    )

    lead_based = {
        "calls_attempted": len(lead_data),
        "picked_calls": len(
            [
                lead
                for lead in lead_data
                if lead["finished_calls"] > lead["no_answer_calls"]
            ]
        ),
        "confirmed_address": len(
            [lead for lead in lead_data if lead["confirmed_calls"] > 0]
        ),
        "requested_cancellation": len(
            [lead for lead in lead_data if lead["cancelled_calls"] > 0]
        ),
        "address_updated": len(
            [lead for lead in lead_data if lead["address_update_calls"] > 0]
        ),
    }

    analytics = {"call_based": call_based, "lead_based": lead_based}

    return JSONResponse(content=analytics)


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


@router.get("/breeze/order-confirmation/call-details", include_in_schema=False)
async def get_call_details(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 10,
    outcome: Optional[str] = None,
    order_id: Optional[str] = None,
    shop_name: Optional[str] = None,
    session: dict = Depends(get_breeze_buddy_session),
):
    """
    Provides paginated call details for the dashboard.
    """
    if not session:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Validate pagination parameters
    if page_size < 1 or page_size > 100:
        raise HTTPException(
            status_code=400, detail="Page size must be between 1 and 100"
        )

    try:
        start_datetime = (
            datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
            if start_date
            else None
        )
        end_datetime = (
            (
                datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
                + timedelta(days=1)
            )
            if end_date
            else None
        )
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date format. Expected ISO format (YYYY-MM-DD): {str(e)}",
        ) from e

    total_items = await get_lead_call_trackers_count(
        start_date=start_datetime,
        end_date=end_datetime,
        outcome=outcome,
        order_id=order_id,
        shop_name=shop_name,
    )

    trackers = await get_all_lead_call_trackers(
        start_date=start_datetime,
        end_date=end_datetime,
        outcome=outcome,
        order_id=order_id,
        shop_name=shop_name,
        page=page,
        page_size=page_size,
    )

    total_pages = (total_items + page_size - 1) // page_size

    items = []
    for t, calling_provider in trackers:
        items.append(
            {
                "id": t.id,
                "order_id": t.payload.get("order_id"),
                "customer_name": t.payload.get("customer_name"),
                "shop_name": t.payload.get("shop_name"),
                "customer_mobile_number": t.payload.get("customer_mobile_number"),
                "outcome": t.outcome.value if t.outcome else "N/A",
                "created_at": t.call_initiated_time,
                "call_id": t.call_id,
                "recording_url": t.recording_url,
                "transcript": (t.metaData.get("transcription") if t.metaData else None),
                "calling_provider": calling_provider,
                "attempt_count": t.attempt_count,
            }
        )

    return {
        "total_items": total_items,
        "total_pages": total_pages,
        "page": page,
        "page_size": page_size,
        "items": items,
    }
