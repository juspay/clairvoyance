import uvicorn
import json
import subprocess
import uuid
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, WebSocket, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyMeetingTokenParams, DailyMeetingTokenProperties

# Database imports
from app.database import init_db_pool, close_db_pool, get_db_connection

# Import necessary components from the new structure
from app.ws.live_session import handle_websocket_session, get_active_connections, get_shutdown_event
from app.core.logger import logger
from app.core.config import DAILY_API_KEY, DAILY_API_URL, PORT, HOST
from app.core.security.jwt import get_current_user
from app import __version__
from app.schemas import AutomaticVoiceUserConnectRequest, VoiceReconnectRequest, TokenData
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.agents.voice.breeze_buddy.breeze.order_confirmation.websocket_bot import main as telephony_websocket_conn
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Connect, Stream
from starlette.websockets import WebSocketDisconnect
from app.core.config import (
    TWILIO_ACCOUNT_SID,
    TWILIO_AUTH_TOKEN,
    TWILIO_FROM_NUMBER,
    TWILIO_WEBSOCKET_URL,
    BREEZE_BUDDY_CALL_PROVIDER,
)
from app.schemas import CallStatus, RequestedBy
from app.database.accessor.main import create_call_data
from uuid import uuid4
from datetime import datetime
from app.services.call_queue_manager import call_queue_manager
from app.agents.voice.automatic.conversation_manager import get_conversation_manager

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

# Store Daily API helpers
daily_helpers = {}


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
    logger.info("Daily REST helper initialized.")
    
    # Conversation database tables are now initialized via init-db.sql
    logger.info("Normalized conversation database structure available via schema initialization.")
    
    # Initialize conversation manager for the main server
    from app.agents.voice.automatic.conversation_manager import start_conversation_manager
    await start_conversation_manager()
    logger.info("Conversation manager initialized for main server.")
    
    yield
    
    logger.info("Application shutdown event triggered...")
    # Cleanup bot processes
    cleanup()
    # Stop conversation manager
    from app.agents.voice.automatic.conversation_manager import stop_conversation_manager
    await stop_conversation_manager()
    logger.info("Conversation manager stopped.")
    # Close database pool
    await close_db_pool()
    # Close aiohttp session
    await aiohttp_session.close()
    logger.info("Aiohttp session closed.")
    # Gracefully shutdown websocket connections
    await shutdown_server()


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

@app.post("/agent/voice/breeze-buddy/{identity}/order-confirmation")
async def trigger_order_confirmation(
    identity: RequestedBy, 
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

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        raise HTTPException(status_code=500, detail="Twilio credentials are not configured.")

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
            call_payload=call_payload,
            assigned_number=TWILIO_FROM_NUMBER
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

@app.websocket("/agent/voice/breeze-buddy/{serviceIdentifier}/callback/{workflow}")
async def telephony_websocket_handler(serviceIdentifier: str, workflow: str, websocket: WebSocket):
    """
    WebSocket endpoint that accepts a connection and passes it to the
    pipecat bot's main function.
    """

    if serviceIdentifier != "twillio" or workflow != "order-confirmation":
        raise HTTPException(status_code=404, detail="Feature not supported for this service or workflow")
    
    aiohttp_session = aiohttp.ClientSession()
    
    try:
        # The websocket_bot_main function handles the entire
        # lifecycle of the WebSocket connection, including accept().
        await telephony_websocket_conn(websocket, aiohttp_session)
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
        # Always close the aiohttp session to prevent resource leaks
        try:
            await aiohttp_session.close()
            logger.debug("Aiohttp session closed successfully.")
        except Exception as session_close_error:
            logger.warning(f"Error closing aiohttp session: {session_close_error}")
        logger.info("WebSocket client connection closed.")


# WebSocket endpoint for Gemini Live
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await handle_websocket_session(websocket)

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
    MAX_DURATION = 30 * 60
    
    # Use test disconnect duration if provided (for testing reconnection)
    if request.testDisconnectSeconds:
        test_duration = min(request.testDisconnectSeconds, 300)  # Max 5 minutes for safety
        logger.info(f"Creating room with test disconnect after {test_duration} seconds")
        room_exp_time = time.time() + test_duration
        token_exp_time = test_duration
    else:
        room_exp_time = time.time() + MAX_DURATION
        token_exp_time = MAX_DURATION
    
    room = await daily_helpers["rest"].create_room(
        params=DailyRoomParams(
            properties=DailyRoomProperties(
                exp=room_exp_time,
                eject_at_room_exp=True,
            )
        )
    )

    token_params = DailyMeetingTokenParams(
        properties=DailyMeetingTokenProperties(
            eject_after_elapsed=token_exp_time,
        )
    )
    
    token = await daily_helpers["rest"].get_token(
        room.url,
        expiry_time=token_exp_time,
        eject_at_token_exp=True,
        owner=True,
        params=token_params,
    )

    # 3. Use provided session ID or generate new one
    session_id = request.sessionId if request.sessionId else str(uuid.uuid4())
    is_reconnection = bool(request.sessionId)
    
    if is_reconnection:
        logger.bind(session_id=session_id).info(f"Reconnection requested for existing session: {session_id}")
    else:
        logger.bind(session_id=session_id).info(f"Generated new session ID for new voice agent: {session_id}")

    # 4. Build command args list
    bot_file = "app.agents.voice.automatic"
    cmd = [
        "python3", "-m", bot_file,
        "-u", room.url,
        "-t", token,
        "--mode", raw_mode.upper() if raw_mode else None,
        "--session-id", session_id,
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

    return {
        "room_url": room.url, 
        "token": token, 
        "session_id": session_id,
        "is_reconnection": is_reconnection
    }

async def validate_reconnection_tokens(request: VoiceReconnectRequest) -> tuple[str, str]:
    """Validate euler and breeze tokens, return user_id and merchant_id."""
    from app.api.auth import validate_euler_auth, fetch_breeze_token, ValidateEulerAuthStatus, FetchTokenStatus
    
    # Validate Euler token first
    if not request.eulerToken:
        raise HTTPException(status_code=401, detail="Euler token required for conversation access")
    
    euler_result = await validate_euler_auth(request.eulerToken)
    if euler_result.status != ValidateEulerAuthStatus.SUCCESS:
        logger.warning(f"Invalid Euler token for reconnection request: {euler_result}")
        raise HTTPException(status_code=401, detail="Invalid Euler token")
    
    merchant_id = euler_result.merchant_id
    
    # Cross-reference with provided merchant ID
    if request.merchantId and request.merchantId != merchant_id:
        logger.warning(f"Merchant ID mismatch: token={merchant_id}, request={request.merchantId}")
        raise HTTPException(status_code=403, detail="Merchant ID mismatch")
    
    # Validate Breeze token
    if not request.breezeToken:
        raise HTTPException(status_code=401, detail="Breeze token required for conversation access")
    
    breeze_result = await fetch_breeze_token(request.breezeToken)
    if breeze_result.status != FetchTokenStatus.SUCCESS:
        logger.warning(f"Invalid Breeze token for reconnection request: {breeze_result}")
        raise HTTPException(status_code=401, detail="Invalid Breeze token")
    
    # Extract user_id from token or use userName as fallback
    # In a real implementation, you'd decode the breeze token to get user_id
    # For now, using userName as user_id (this should be improved)
    user_id = request.userName or "unknown_user"
    
    logger.info(f"Token validation successful: user_id={user_id}, merchant_id={merchant_id}")
    return user_id, merchant_id


# Voice reconnection endpoint with authentication
@app.post("/agent/voice/automatic/reconnect")
async def reconnect_voice_session(request: VoiceReconnectRequest) -> Dict[str, Any]:
    """Reconnect to an existing voice session with proper authentication and conversation context preservation."""
    session_id = request.sessionId
    logger.info(f"Received reconnection request for session: {session_id}")
    
    # 1. Validate authentication tokens first
    try:
        user_id, merchant_id = await validate_reconnection_tokens(request)
        logger.info(f"Authentication successful for reconnection: user={user_id}, merchant={merchant_id}")
    except HTTPException:
        raise  # Re-raise authentication errors
    except Exception as e:
        logger.error(f"Token validation error: {e}")
        raise HTTPException(status_code=500, detail="Authentication validation failed")
    
    try:
        # 1. Check if conversation exists in conversation manager or database
        conversation_manager = get_conversation_manager()
        logger.info(f"Checking conversation manager for session {session_id}. Active sessions: {conversation_manager.get_session_stats()}")
        
        # First check memory, then database
        conversation = conversation_manager.get_conversation(session_id)
        
        if not conversation:
            logger.info(f"No conversation in memory for session {session_id}, checking normalized database with user authorization...")
            try:
                # Use secure normalized database load with user/merchant authorization
                conversation = await conversation_manager.load_conversation_from_db(session_id, user_id, merchant_id)
                if conversation:
                    logger.info(f"Loaded authorized conversation for session {session_id} from normalized database")
            except Exception as e:
                logger.debug(f"Secure normalized database load failed: {e}")
                conversation = None
        
        if not conversation:
            logger.warning(f"No conversation found in memory or database for session {session_id}")
            logger.info(f"Proceeding with reconnection using existing session ID {session_id} but fresh context")
            conversation_turns = 0
        else:
            logger.info(f"Found existing conversation for session {session_id} with {len(conversation.turns)} turns")
            conversation_turns = len(conversation.turns)
        
        # 2. Create new Daily.co room + token (same logic as new connection)
        MAX_DURATION = 30 * 60
        room = await daily_helpers["rest"].create_room(
            params=DailyRoomParams(
                properties=DailyRoomProperties(
                    exp=time.time() + MAX_DURATION,
                    eject_at_room_exp=True,
                )
            )
        )

        token_params = DailyMeetingTokenParams(
            properties=DailyMeetingTokenProperties(
                eject_after_elapsed=MAX_DURATION,
            )
        )
        
        token = await daily_helpers["rest"].get_token(
            room.url,
            expiry_time=MAX_DURATION,
            eject_at_token_exp=True,
            owner=True,
            params=token_params,
        )
        
        # 3. Launch new subprocess with EXISTING session ID
        bot_file = "app.agents.voice.automatic"
        cmd = [
            "python3", "-m", bot_file,
            "-u", room.url,
            "-t", token,
            "--session-id", session_id,  # Use existing session ID
        ]
        
        # Add optional parameters from reconnect request
        if request.mode:
            cmd += ["--mode", request.mode.upper()]
        if request.userName:
            cmd += ["--user-name", request.userName]
        if request.eulerToken:
            cmd += ["--euler-token", request.eulerToken]
        if request.breezeToken:
            cmd += ["--breeze-token", request.breezeToken]
        if request.shopUrl:
            cmd += ["--shop-url", request.shopUrl]
        if request.shopId:
            cmd += ["--shop-id", request.shopId]
        if request.shopType:
            cmd += ["--shop-type", request.shopType]
        if request.merchantId:
            cmd += ["--merchant-id", request.merchantId]
        if request.platformIntegrations:
            cmd += ["--platform-integrations"] + request.platformIntegrations
        
        # Launch subprocess
        logger.bind(session_id=session_id).info(f"Launching reconnection subprocess with command: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent.parent,
            bufsize=1,
        )
        bot_procs[proc.pid] = (proc, room.url)
        logger.bind(session_id=session_id).info(f"Reconnection subprocess started with PID: {proc.pid}")

        return {
            "room_url": room.url,
            "token": token,
            "session_id": session_id,
            "is_reconnection": True,
            "conversation_turns": conversation_turns,
            "message": f"Successfully reconnected to session {session_id}"
        }
        
    except Exception as e:
        logger.error(f"Error during reconnection for session {session_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "error": "Reconnection failed",
                "message": f"Failed to reconnect session {session_id}: {str(e)}",
                "session_id": session_id
            }
        )


# Serve client.html at the root
@app.get("/")
async def get_client_html():
    return FileResponse("static/client.html")

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

# Conversation history endpoint for reconnection
@app.get("/conversation/{session_id}")
async def get_conversation_history(session_id: str):
    """Get conversation history for a session to support reconnection."""
    try:
        logger.info(f"Fetching conversation history for session: {session_id}")
        
        conversation_manager = get_conversation_manager()
        conversation_data = conversation_manager.export_conversation(session_id)
        
        if conversation_data:
            logger.info(f"Found conversation history for session {session_id} with {len(conversation_data.get('turns', []))} turns")
            return JSONResponse({
                "success": True,
                "conversation": conversation_data,
                "session_id": session_id
            })
        else:
            logger.warning(f"No conversation history found for session: {session_id}")
            return JSONResponse({
                "success": False,
                "conversation": None,
                "session_id": session_id,
                "message": "No conversation history found for this session"
            })
            
    except Exception as e:
        logger.error(f"Error fetching conversation history for session {session_id}: {e}")
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "conversation": None,
                "session_id": session_id,
                "error": f"Failed to fetch conversation history: {str(e)}"
            }
        )

# Graceful shutdown handling for WebSocket connections
async def shutdown_server():
    logger.info("Shutdown initiated, closing all WebSocket connections...")
    shutdown_event = get_shutdown_event()
    shutdown_event.set()
    
    active_connections = get_active_connections()
    # Close all active WebSockets
    for ws in list(active_connections): # Iterate over a copy
        try:
            await ws.close(code=1001, reason="Server shutting down")
            if ws in active_connections:
                active_connections.remove(ws)
            logger.info(f"Closed WebSocket connection: {ws.client}")
        except Exception as e:
            logger.error(f"Error closing websocket during shutdown: {e}")
    
    logger.info("All WebSocket connections closed.")

# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
