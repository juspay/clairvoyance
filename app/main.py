import uvicorn
import subprocess
import uuid
import time
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

import aiohttp
from fastapi import FastAPI, WebSocket, HTTPException, Request, Depends, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyMeetingTokenParams, DailyMeetingTokenProperties
from starlette.websockets import WebSocketDisconnect

# Database imports

from app.database import init_db_pool, close_db_pool, get_db_connection
from app.database.queries.daily_hotline import get_pool_stats_query
from uuid import UUID
# Import necessary components from the new structure
from app.core.logger import logger
from app.core.security.jwt import get_current_user
from app import __version__
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.agents.voice.breeze_buddy.breeze.order_confirmation.websocket_bot import main as telephony_websocket_conn
from app.core.config import (
    DAILY_API_KEY,
    DAILY_API_URL,
    PORT,
    HOST,
    BREEZE_BUDDY_CALL_PROVIDER,
    MAX_DAILY_SESSION_LIMIT,
    ENABLE_AUTOMATIC_DAILY_RECORDING,
    EXOTEL_FROM_NUMBER,
    TWILIO_FROM_NUMBER,
    ENABLE_DAILY_HOTLINE, 
    DAILY_HOTLINE_CLEANUP_ON_SHUTDOWN
)
from app.core.security.jwt import get_current_user
from app import __version__
from app.schemas import AutomaticVoiceUserConnectRequest, TokenData, CallStatus, RequestedBy, Workflow
from app.services.call_queue_manager import CallQueueManager
from app.database.accessor.main import create_call_data
from uuid import uuid4
from datetime import datetime
from app.services.automatic.daily.hotline_manager import initialize_hotline_manager, get_hotline_manager
from app.database.queries.daily_hotline import release_room_query

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

def spawn_hotline_agent(room_url: str, token: str, request_params: Dict[str, Any]) -> Optional[int]:
    """
    Spawn hotline agent subprocess from main application layer.
    This centralizes subprocess management in main.py as recommended.
    
    Args:
        room_url: Daily room URL for the agent to join
        token: Daily room token for authentication
        request_params: Dictionary containing agent configuration
        
    Returns:
        int: Process PID if successful, None if failed
    """
    try:
        # Build command args for hotline agent
        bot_file = "app.agents.voice.automatic"
        cmd = [
            "python3", "-m", bot_file,
            "-u", room_url,
            "-t", token,
            "--mode", request_params.get("mode", "TEST").upper(),
            "--session-id", f"hotline-{int(time.time())}",
        ]
        
        # Add optional parameters
        for param, arg in [
            ("user_name", "--user-name"),
            ("tts_provider", "--tts-provider"),
            ("voice_name", "--voice-name"),
            ("euler_token", "--euler-token"),
            ("breeze_token", "--breeze-token"),
            ("shop_url", "--shop-url"),
            ("shop_id", "--shop-id"),
            ("merchant_id", "--merchant-id"),
            ("shop_type", "--shop-type"),
        ]:
            if request_params.get(param):
                cmd.extend([arg, str(request_params[param])])
        
        # Add platform integrations if present
        platform_integrations = request_params.get("platform_integrations")
        if platform_integrations:
            cmd += ["--platform-integrations"] + platform_integrations
        
        # Launch subprocess
        logger.info(f"HOTLINE SPAWN: Launching agent subprocess with command: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent.parent,
            bufsize=1,
        )
        
        # Track the process
        bot_procs[proc.pid] = (proc, room_url)
        logger.info(f"HOTLINE SPAWN SUCCESS: Agent subprocess started with PID {proc.pid}")
        
        return proc.pid
        
    except Exception as e:
        logger.error(f"HOTLINE SPAWN ERROR: Failed to spawn agent subprocess: {e}")
        return None

# Store Daily API helpers
daily_helpers = {}
call_queue_manager: CallQueueManager

# Global set to track active WebSocket connections
active_websocket_connections = set()

# Global background task reference to prevent garbage collection
_background_tasks = []


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
    
    # Initialize hotline manager if enabled
    if ENABLE_DAILY_HOTLINE:
        logger.info("Daily Hotline system enabled, initializing room pool manager...")
        hotline_manager = initialize_hotline_manager(daily_helpers["rest"], spawn_hotline_agent)
        # Start background pool management task with proper reference management
        pool_task = asyncio.create_task(hotline_manager.manage_pool())
        pool_task.set_name("daily_hotline_pool_management")
        _background_tasks.append(pool_task)  # Keep reference to prevent GC
        logger.info("Daily Hotline manager initialized and pool management started.")
    else:
        logger.info("Daily Hotline system disabled, using legacy on-demand room creation.")
    
    yield
    
    logger.info("Application shutdown event triggered...")
    
    # Cancel background tasks
    for task in _background_tasks:
        if not task.done():
            logger.info(f"Cancelling background task: {task.get_name()}")
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                logger.info(f"Background task {task.get_name()} cancelled successfully")
    
    # Cleanup hotline agents if enabled
    if ENABLE_DAILY_HOTLINE and DAILY_HOTLINE_CLEANUP_ON_SHUTDOWN:
        hotline_manager = get_hotline_manager()
        if hotline_manager:
            await hotline_manager.cleanup_all_agents()
    elif ENABLE_DAILY_HOTLINE and not DAILY_HOTLINE_CLEANUP_ON_SHUTDOWN:
        logger.debug("Daily Hotline cleanup on shutdown is disabled, skipping agent cleanup")
    
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


# WebSocket endpoint for Gemini Live
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await handle_websocket_session(websocket)


@app.get("/hotline/status")
async def hotline_status():
    """Get hotline system status including pool management task status."""
    if not ENABLE_DAILY_HOTLINE:
        return {"enabled": False, "message": "Daily Hotline system disabled"}
    
    hotline_manager = get_hotline_manager()
    if not hotline_manager:
        return {"enabled": True, "status": "error", "message": "Hotline manager not initialized"}
    
    try:
        # Check if pool management is running
        is_managing = hotline_manager._is_managing_pool
        
        # Get current pool stats
        async for conn in get_db_connection():
            query, values = get_pool_stats_query()
            result = await conn.fetchrow(query, *values)
            stats = dict(result) if result else {}
            break
        
        # Check if there are any active asyncio tasks with our name
        active_tasks = [task for task in asyncio.all_tasks() if task.get_name() == "hotline_pool_management"]
        
        return {
            "enabled": True,
            "pool_management_running": is_managing,
            "background_tasks_count": len(active_tasks),
            "pool_stats": stats,
            "startup_cleanup_done": hotline_manager._startup_cleanup_done
        }
        
    except Exception as e:
        return {"enabled": True, "status": "error", "message": f"Error checking status: {str(e)}"}


# Health check endpoints

# Pipecat bot endpoint
@app.post("/agent/voice/automatic")
async def bot_connect(request: AutomaticVoiceUserConnectRequest) -> Dict[str, Any]:
    start_time = time.time()
    logger.info(f"Received new user connect request payload: {request.model_dump_json(exclude_none=True)}")
    
    # Extract request parameters
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

    # Try daily hotline first if enabled
    if ENABLE_DAILY_HOTLINE:
        logger.debug("DAILY HOTLINE ATTEMPT: Trying to get pre-allocated room from database pool...")
        try:
            hotline_manager = get_hotline_manager()
            if hotline_manager:
                # Generate session ID for room tracking
                session_id = str(uuid.uuid4())
                logger.info(f"Generated session ID for hotline room: {session_id}")
                
                # Build request params for hotline
                request_params = {
                    "mode": raw_mode,
                    "user_name": user_name,
                    "tts_provider": tts_provider,
                    "voice_name": voice_name,
                    "euler_token": euler_tok,
                    "breeze_token": breeze_tok,
                    "shop_url": shop_url,
                    "shop_id": shop_id,
                    "shop_type": shop_type,
                    "merchant_id": merchant_id,
                    "platform_integrations": platform_integrations,
                }
                
                reserved_room = await hotline_manager.get_reserved_room(request_params, session_id)
                if reserved_room:
                    elapsed_time = (time.time() - start_time) * 1000
                    logger.info(f"HOTLINE SUCCESS: Got pre-allocated room from DB pool in {elapsed_time:.2f}ms for session {session_id}")
                    return {
                        "room_url": reserved_room["room_url"],
                        "token": reserved_room["token"],
                        "session_id": session_id  # Return session_id for client tracking
                    }
                else:
                    logger.warning("HOTLINE FALLBACK: No available rooms in pool, falling back to on-demand creation")
            else:
                logger.warning("HOTLINE FALLBACK: Manager not initialized, falling back to on-demand creation")
        except Exception as e:
            error_type = type(e).__name__
            logger.error(f"HOTLINE ERROR ({error_type}): {e}")
            
            # Don't fallback on client errors or authentication issues
            if isinstance(e, (ValueError, TypeError, KeyError)):
                raise HTTPException(status_code=400, detail=f"Invalid request parameters: {str(e)}")
            elif "authentication" in str(e).lower() or "unauthorized" in str(e).lower():
                raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")
            else:
                # For infrastructure issues, fallback to legacy
                logger.warning(f"HOTLINE FALLBACK: Infrastructure issue, falling back to on-demand creation: {e}")
    
    # Legacy flow (original implementation)
    logger.info("ON-DEMAND CREATION: Creating new Daily.co room and spawning agent process...")
    return await legacy_bot_connect(request, start_time)


async def legacy_bot_connect(request: AutomaticVoiceUserConnectRequest, start_time: float) -> Dict[str, Any]:
    """Legacy on-demand room creation flow."""
    # Extract request parameters
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
    
    try:

    # 2. Create room + token
    
        daily_room_properties = DailyRoomProperties(
            exp=time.time() + MAX_DAILY_SESSION_LIMIT,
            eject_at_room_exp=True,
        )
    
        # Enable recording only if configured
        if ENABLE_AUTOMATIC_DAILY_RECORDING:
            daily_room_properties.enable_recording = "cloud"

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
        # 3. Generate unique session ID for this subprocess
        session_id = request.sessionId or str(uuid.uuid4())
        client_sid = request.sessionId or str(uuid.uuid4())  
        logger.bind(session_id=session_id).info(f"Using session ID for new voice agent: {session_id}")
        logger.bind(client_sid=client_sid).info(f"Using client session ID for new voice agent: {client_sid}")

        # Build command args list
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

        # Launch subprocess without shell
        logger.bind(session_id=session_id).info(f"Launching subprocess with command: {' '.join(cmd)}")
        proc = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent.parent,
            bufsize=1,
        )
        bot_procs[proc.pid] = (proc, room.url)
        logger.bind(session_id=session_id).info(f"Subprocess started with PID: {proc.pid}")

        elapsed_time = (time.time() - start_time) * 1000
        logger.info(f"ON-DEMAND SUCCESS: Created room and spawned agent in {elapsed_time:.2f}ms")
        
        return {"room_url": room.url, "token": token}
        
    except Exception as e:
        logger.error(f"Error in legacy bot connect: {e}")
        # Determine if this is a client error (4XX) or server error (5XX)
        error_message = str(e)
        if "validation" in error_message.lower() or "invalid" in error_message.lower():
            raise HTTPException(status_code=400, detail=f"Invalid request: {str(e)}")
        elif "unauthorized" in error_message.lower() or "forbidden" in error_message.lower():
            raise HTTPException(status_code=401, detail=f"Authentication failed: {str(e)}")
        else:
            # Daily API failures, database issues, subprocess failures are server errors
            raise HTTPException(status_code=400, detail=f"Failed to create room: {str(e)}")


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


# Hotline status endpoint
@app.get("/hotline/status")
async def hotline_status():
    """Get daily hotline pool status for monitoring."""
    if not ENABLE_DAILY_HOTLINE:
        return JSONResponse({
            "daily_hotline_enabled": False,
            "message": "Daily Hotline system is disabled"
        })
    
    try:
        hotline_manager = get_hotline_manager()
        if not hotline_manager:
            return JSONResponse({
                "daily_hotline_enabled": True,
                "error": "Daily hotline manager not initialized"
            })
        
        status = await hotline_manager.get_pool_status()
        status["daily_hotline_enabled"] = True
        return JSONResponse(status)
    except Exception as e:
        logger.error(f"Error getting hotline status: {e}")
        return JSONResponse({
            "hotline_enabled": True,
            "error": str(e)
        })

@app.post("/hotline/release/{room_id}")
async def release_hotline_room(room_id: str, current_user: TokenData = Depends(get_current_user)):
    """Release a daily hotline room back to available status. Requires JWT authentication."""
    if not ENABLE_DAILY_HOTLINE:
        raise HTTPException(status_code=404, detail="Daily Hotline system is disabled")
    try:
        room_uuid = UUID(room_id)
        async for conn in get_db_connection():
            query, values = release_room_query(room_uuid)
            result = await conn.execute(query, *values)
            rows_affected = int(result.split()[-1])
            released = rows_affected > 0
            if released:
                logger.debug(f"User {current_user.user_id} released hotline room {room_id}")
                return {"status": "released", "room_id": room_id}
            else:
                return {"status": "not_found", "room_id": room_id}
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid room ID format")
    except Exception as e:
        logger.error(f"Error releasing room {room_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Graceful shutdown handling for WebSocket connections
async def shutdown_server():
    logger.info("Shutdown initiated, closing all WebSocket connections...")
    shutdown_event = get_shutdown_event()
    shutdown_event.set()
    
    # Close all active WebSockets
    for ws in list(active_websocket_connections):  # Iterate over a copy
        try:
            await ws.close(code=1001, reason="Server shutting down")
            active_websocket_connections.discard(ws)
            logger.info(f"Closed WebSocket connection: {ws.client}")
        except Exception as e:
            logger.error(f"Error closing websocket during shutdown: {e}")
    
    logger.info("All WebSocket connections closed.")

# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
