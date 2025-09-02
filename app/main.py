import uvicorn
import subprocess
import uuid
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, WebSocket, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyMeetingTokenParams, DailyMeetingTokenProperties
from starlette.websockets import WebSocketDisconnect

# Database imports
from app.database import init_db_pool, close_db_pool, get_db_connection

# Import necessary components from the new structure
from app.core.logger import logger
from app.core.config import (
    DAILY_API_KEY,
    DAILY_API_URL,
    PORT,
    HOST,
    BREEZE_BUDDY_CALL_PROVIDER,
    MAX_DAILY_SESSION_LIMIT,
    ENABLE_AUTOMATIC_DAILY_RECORDING,
    EXOTEL_FROM_NUMBER,
    TWILIO_FROM_NUMBER
)
from app.core.security.jwt import get_current_user
from app import __version__
from app.schemas import AutomaticVoiceUserConnectRequest, TokenData, CallStatus, RequestedBy, Workflow
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.services.call_queue_manager import CallQueueManager
from app.database.accessor.main import create_call_data
from uuid import uuid4
from datetime import datetime

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

# Store Daily API helpers
daily_helpers = {}
call_queue_manager: CallQueueManager


def cleanup():
    """Cleanup function to terminate all bot processes.

    Called during server shutdown.
    """
    logger.info(f"Attempting to terminate {len(bot_procs)} bot processes.")
    for pid, (proc, room_url) in list(bot_procs.items()):
        try:
            if proc.poll() is None:
                logger.info(f"Terminating process {pid} for room {room_url}...")
                proc.terminate()
                proc.wait()
                logger.info(f"Process {pid} terminated successfully.")
            else:
                logger.info(f"Process {pid} for room {room_url} has already terminated.")
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}", exc_info=True)
        finally:
            # Ensure the process is removed from the tracking dictionary
            bot_procs.pop(pid, None)
    logger.info("All bot processes have been handled.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    global call_queue_manager
    logger.info("Application startup...")
    
    # Initialize database and create tables if needed
    try:
        await init_db_pool()
        logger.info("Database initialized successfully with schema.")
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")
    
    # Initialize aiohttp session
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=DAILY_API_KEY,
        daily_api_url=DAILY_API_URL,
        aiohttp_session=aiohttp_session,
    )
    call_queue_manager = CallQueueManager(aiohttp_session)
    logger.info("Daily REST helper initialized.")
    
    yield
    
    logger.info("Application shutdown event triggered...")
    # Cleanup bot processes
    cleanup()
    # Close database pool
    await close_db_pool()
    # Close aiohttp session
    await aiohttp_session.close()
    logger.info("Aiohttp session closed.")


app = FastAPI(title="Breeze Automatic Server", version=__version__, lifespan=lifespan)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.post("/agent/voice/breeze-buddy/{identity}/{workflow}")
async def trigger_order_confirmation(
    identity: RequestedBy,
    workflow: Workflow,
    order: BreezeOrderData,
    current_user: TokenData = Depends(get_current_user)
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

@app.websocket("/agent/voice/breeze-buddy/{service_provider}/callback/{workflow}")
async def telephony_websocket_handler(service_provider: str, workflow: str, websocket: WebSocket):
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


# Pipecat bot endpoint
@app.post("/agent/voice/automatic")
async def bot_connect(request: AutomaticVoiceUserConnectRequest) -> Dict[str, Any]:
    logger.info(f"Received new user connect request payload: {request.model_dump_json(exclude_none=True)}")
    # 1. Validate request
    raw_mode = request.mode
    euler_tok = request.eulerToken
    breeze_tok = request.breezeToken
    shop_url = request.shopUrl
    shop_id = request.shopId
    shop_type = request.shopType
    user_name = request.userName
    tts_provider = request.ttsService.ttsProvider.value if request.ttsService else None
    voice_name = request.ttsService.voiceName.value if request.ttsService else None
    merchant_id = request.merchantId
    platform_integrations = request.platformIntegrations

    # 2. Create room + token
    
    daily_room_properties = DailyRoomProperties(
        exp=time.time() + MAX_DAILY_SESSION_LIMIT,
        eject_at_room_exp=True,
    )
    
    # Enable recording only if configured
    if ENABLE_AUTOMATIC_DAILY_RECORDING:
        daily_room_properties.enable_recording = "cloud"
    
    room = await daily_helpers["rest"].create_room(
        params=DailyRoomParams(properties=daily_room_properties)
    )

    token_params = DailyMeetingTokenParams(
        properties=DailyMeetingTokenProperties(
            eject_after_elapsed=MAX_DAILY_SESSION_LIMIT,
        )
    )
    
    token = await daily_helpers["rest"].get_token(
        room.url,
        expiry_time=MAX_DAILY_SESSION_LIMIT,
        eject_at_token_exp=True,
        owner=True,
        params=token_params,
    )

    # 3. Generate unique session ID and client session ID for this subprocess
    session_id = str(uuid.uuid4())  # Always generate random session ID
    client_sid = request.sessionId or str(uuid.uuid4())  # Use client-provided sessionId or generate fallback
    logger.bind(session_id=session_id).info(f"Generated session ID for new voice agent: {session_id}")
    logger.bind(client_sid=client_sid).info(f"Using client session ID for new voice agent: {client_sid}")

    # 4. Build command args list
    bot_file = "app.agents.voice.automatic"
    cmd = [
        "python3", "-m", bot_file,
        "-u", room.url,
        "-t", token,
        "--mode", raw_mode.upper() if raw_mode else None,
        "--session-id", session_id,
        "--client-sid", client_sid,
    ]

    # Add user_name and tts_service regardless of mode
    if user_name:
        cmd += ["--user-name", user_name]
    if tts_provider:
        cmd += ["--tts-provider", tts_provider]
    if voice_name:
        cmd += ["--voice-name", voice_name]
    if euler_tok:
        cmd += ["--euler-token", euler_tok]
    if breeze_tok:
        cmd += ["--breeze-token", breeze_tok]
    if shop_url:
        cmd += ["--shop-url", shop_url]
    if shop_id:
        cmd += ["--shop-id", shop_id]
    if shop_type:
        cmd += ["--shop-type", shop_type]
    if merchant_id:
        cmd += ["--merchant-id", merchant_id]
    if platform_integrations:
        cmd += ["--platform-integrations"] + platform_integrations

    # 5. Launch subprocess without shell
    logger.bind(session_id=session_id).info(f"Launching subprocess with command: {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        cwd=Path(__file__).parent.parent,
        bufsize=1,
    )
    bot_procs[proc.pid] = (proc, room.url)
    logger.bind(session_id=session_id).info(f"Subprocess started with PID: {proc.pid}")

    return {"room_url": room.url, "token": token}


# Serve client.html at the root
@app.get("/")
async def get_client_html():
    return FileResponse("static/home.html")

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return JSONResponse({"status": "healthy"})

# Database health check endpoint
@app.get("/health/database")
async def database_health_check():
    """Check database connectivity and health."""
    logger.info("Database health check endpoint called")
    try:
        async for conn in get_db_connection():
            result = await conn.fetchval("SELECT 1")
            if result == 1:
                return JSONResponse({
                    "status": "healthy",
                    "database": "connected",
                    "message": "Database connection is healthy"
                })
            else:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "unhealthy",
                        "database": "error",
                        "message": "Database query returned unexpected result"
                    }
                )
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "message": f"Database connection failed: {str(e)}"
            }
        )

# Version endpoint
@app.get("/version")
async def get_version():
    """Get application version."""
    return JSONResponse({"version": __version__})

# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
