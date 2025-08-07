import uvicorn
import json
import uuid
import time
import asyncio
from contextlib import asynccontextmanager
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams, DailyRoomProperties, DailyMeetingTokenParams, DailyMeetingTokenProperties

# Import necessary components from the new structure
from app.ws.live_session import handle_websocket_session, get_active_connections, get_shutdown_event
from app.core.logger import logger
from app.core.config import DAILY_API_KEY, DAILY_API_URL, PORT, HOST
from app import __version__
from app.schemas import AutomaticVoiceUserConnectRequest
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
)

# Import new services
from app.services.session_manager import session_manager
from app.services.model_manager import shared_model_manager
from app.core.redis_manager import redis_manager
from app.services.monitoring import performance_monitor
from app.core.latency_tracker import latency_tracker

# Store Daily API helpers
daily_helpers = {}


async def cleanup():
    """Cleanup function for new worker-based architecture.

    Called during server shutdown.
    """
    logger.info("Starting cleanup of worker-based architecture...")
    
    try:
        # Stop performance monitoring
        await performance_monitor.stop()
        
        # Stop session manager (which will stop worker pool)
        await session_manager.stop()
        
        # Cleanup shared models
        await shared_model_manager.cleanup()
        
        # Disconnect from Redis
        await redis_manager.disconnect()
        
        logger.info("Worker-based architecture cleanup completed.")
    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    logger.info("Application startup...")
    
    # Initialize Redis connection
    await redis_manager.connect()
    logger.info("Redis connection established.")
    
    # Initialize shared models
    await shared_model_manager.initialize()
    logger.info("Shared models initialized.")
    
    # Start session manager (which starts worker pool)
    await session_manager.start()
    logger.info("Session manager and worker pool started.")
    
    # Start performance monitoring
    await performance_monitor.start()
    logger.info("Performance monitoring started.")
    
    # Initialize aiohttp session for Daily API
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=DAILY_API_KEY,
        daily_api_url=DAILY_API_URL,
        aiohttp_session=aiohttp_session,
    )
    logger.info("Daily REST helper initialized.")
    
    yield
    
    logger.info("Application shutdown event triggered...")
    
    # Cleanup worker-based architecture
    await cleanup()
    
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
async def trigger_order_confirmation(identity: str, order: BreezeOrderData):
    """
    Receives order details and triggers a order confirmation workflow.
    """
    if identity != "breeze":
        raise HTTPException(status_code=404, detail="Feature not supported")
    
    logger.info(f"Received order: {order.order_id} for {order.customer_name}")

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        raise HTTPException(status_code=500, detail="Twilio credentials are not configured.")

    client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
    
    ws_url = TWILIO_WEBSOCKET_URL

    voice_call_payload = VoiceResponse()
    connect = Connect()
    stream = Stream(url=ws_url)
    stream.parameter(name="order_id", value=order.order_id)
    stream.parameter(name="customer_name", value=order.customer_name)
    stream.parameter(name="shop_name", value=order.shop_name)
    stream.parameter(name="total_price", value=order.total_price)
    stream.parameter(name="customer_address", value=order.customer_address)
    stream.parameter(name="customer_mobile_number", value=order.customer_mobile_number)
    stream.parameter(name="order_data", value=json.dumps(order.order_data.model_dump()))
    stream.parameter(name="identity", value=identity)
    if order.reporting_webhook_url:
        stream.parameter(name="reporting_webhook_url", value=order.reporting_webhook_url)
    connect.append(stream)
    voice_call_payload.append(connect)

    try:
        call = client.calls.create(
            to=order.customer_mobile_number,
            from_=TWILIO_FROM_NUMBER,
            twiml=str(voice_call_payload)
        )
        logger.info(f"Call initiated with SID: {call.sid}")
        return {"status": "call_initiated", "sid": call.sid}
    except Exception as e:
        logger.error(f"Failed to initiate call: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.websocket("/agent/voice/breeze-buddy/{serviceIdentifier}/callback/{workflow}")
async def telephony_websocket_handler(serviceIdentifier: str, workflow: str, websocket: WebSocket):
    """
    WebSocket endpoint for telephony using new worker-based architecture.
    """
    
    if serviceIdentifier != "twillio" or workflow != "order-confirmation":
        raise HTTPException(status_code=404, detail="Feature not supported for this service or workflow")
    
    try:
        # Accept the connection
        await websocket.accept()
        
        config = {
            "service_identifier": serviceIdentifier,
            "workflow": workflow,
        }
        
        # Create session via session manager
        from app.services.session_manager import SessionType
        session_id = await session_manager.create_websocket_session(
            websocket=websocket,
            session_type=SessionType.TELEPHONY,
            config=config
        )
        
        logger.info(f"Created telephony WebSocket session {session_id}")
        
        # Keep connection alive until worker handles it
        try:
            while True:
                # Worker will handle the actual telephony bot logic
                await asyncio.sleep(1)
        except Exception as e:
            logger.info(f"Telephony session {session_id} ended: {e}")
            
    except WebSocketDisconnect:
        logger.warning("WebSocket client disconnected.")
    except Exception as e:
        logger.error(f"An error occurred in the WebSocket handler: {e}")
        if websocket.client_state.name == 'CONNECTED':
            await websocket.close(code=1011, reason="Internal Server Error")
    finally:
        # Cleanup handled by session manager
        await session_manager.websocket_disconnected(websocket)
        logger.info("WebSocket client connection closed.")


# WebSocket endpoint for Gemini Live
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint using new worker-based architecture."""
    connection_start = time.time()
    session_id = None
    
    try:
        # Track WebSocket handshake
        async with latency_tracker.track_async("websocket_connection", "websocket_handshake", {
            "endpoint": "/ws/live",
            "client": str(websocket.client) if websocket.client else "unknown"
        }):
            await websocket.accept()
        
        # Extract session config from query params
        token = websocket.query_params.get("token")
        testmode_param = websocket.query_params.get("testmode", "false").lower()
        is_test_mode = testmode_param == "true"
        
        config = {
            "token": token,
            "test_mode": is_test_mode,
            "use_dummy_data": is_test_mode or not token
        }
        
        # Create session via session manager with latency tracking
        from app.services.session_manager import SessionType
        
        async with latency_tracker.track_async("websocket_connection", "live_session_creation", {
            "test_mode": is_test_mode,
            "has_token": bool(token)
        }):
            session_id = await session_manager.create_websocket_session(
                websocket=websocket,
                session_type=SessionType.LIVE,
                config=config
            )
        
        connection_latency = (time.time() - connection_start) * 1000
        logger.info(
            f"[LATENCY] Live WebSocket session established | Session: {session_id} | "
            f"Connection: {connection_latency:.2f}ms",
            extra={
                "session_id": session_id,
                "connection_latency_ms": connection_latency,
                "session_type": "live",
                "test_mode": is_test_mode,
                "client": str(websocket.client) if websocket.client else "unknown"
            }
        )
        
        # Track session activity
        latency_tracker.start_event(session_id, "session_active", {
            "session_type": "live",
            "test_mode": is_test_mode
        })
        
        # Keep connection alive until worker handles it
        try:
            while True:
                # Worker will handle the actual session logic
                # This just keeps the WebSocket open
                await asyncio.sleep(1)
        except Exception as e:
            session_duration = (time.time() - connection_start) * 1000
            logger.info(
                f"[LATENCY] Live WebSocket session ended | Session: {session_id} | "
                f"Duration: {session_duration:.2f}ms | Reason: {e}",
                extra={
                    "session_id": session_id,
                    "session_duration_ms": session_duration,
                    "end_reason": str(e)
                }
            )
        
    except Exception as e:
        connection_latency = (time.time() - connection_start) * 1000
        logger.error(
            f"[LATENCY] WebSocket endpoint error | Session: {session_id} | "
            f"Duration: {connection_latency:.2f}ms | Error: {e}",
            extra={
                "session_id": session_id,
                "connection_latency_ms": connection_latency,
                "error": str(e),
                "websocket_error": True
            }
        )
        if websocket.client_state.name == 'CONNECTED':
            await websocket.close(code=1011, reason="Internal Server Error")
    finally:
        # Cleanup handled by session manager
        if session_id:
            await session_manager.websocket_disconnected(websocket)
            latency_tracker.cleanup_session(session_id)

# Pipecat bot endpoint
@app.post("/agent/voice/automatic")
async def bot_connect(request: AutomaticVoiceUserConnectRequest) -> Dict[str, Any]:
    # Generate session ID early for tracking
    session_id = str(uuid.uuid4())
    request_start = time.time()
    
    logger.info(
        f"[LATENCY] New automatic voice request | Session: {session_id}",
        extra={
            "session_id": session_id,
            "request_payload": request.model_dump_json(exclude_none=True),
            "request_start_time": request_start
        }
    )
    
    async with latency_tracker.track_async(session_id, "session_creation", {
        "session_type": "automatic",
        "mode": request.mode,
        "user_name": request.userName,
        "tts_provider": request.ttsService.ttsProvider.value if request.ttsService else None
    }):
        
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

        # 2. Create room + token with latency tracking
        MAX_DURATION = 30 * 60
        
        async with latency_tracker.track_async(session_id, "room_creation", {"service": "daily"}):
            room = await daily_helpers["rest"].create_room(
                params=DailyRoomParams(
                    properties=DailyRoomProperties(
                        exp=time.time() + MAX_DURATION,
                        eject_at_room_exp=True,
                    )
                )
            )

        async with latency_tracker.track_async(session_id, "token_generation", {"service": "daily"}):
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

        # 3. Build configuration for worker
        config = {
            "room_url": room.url,
            "token": token,
            "mode": raw_mode.upper() if raw_mode else None,
            "session_id": session_id,
            "user_name": user_name,
            "tts_provider": tts_provider,
            "voice_name": voice_name,
            "euler_token": euler_tok,
            "breeze_token": breeze_tok,
            "shop_url": shop_url,
            "shop_id": shop_id,
            "shop_type": shop_type,
            "merchant_id": merchant_id,
            "platform_integrations": platform_integrations
        }

        # 4. Create session via session manager with latency tracking
        try:
            async with latency_tracker.track_async(session_id, "worker_allocation", {
                "worker_pool_size": 4,  # This could be dynamic
                "session_type": "automatic"
            }):
                result = await session_manager.create_automatic_session(config)
            
            total_latency = (time.time() - request_start) * 1000
            logger.info(
                f"[LATENCY] Automatic session created successfully | Session: {session_id} | "
                f"Total: {total_latency:.2f}ms",
                extra={
                    "session_id": session_id,
                    "total_latency_ms": total_latency,
                    "room_url": room.url,
                    "session_creation_success": True
                }
            )
            
            return {"room_url": room.url, "token": token, "session_id": session_id}
            
        except Exception as e:
            total_latency = (time.time() - request_start) * 1000
            logger.error(
                f"[LATENCY] Failed to create automatic session | Session: {session_id} | "
                f"Total: {total_latency:.2f}ms | Error: {e}",
                extra={
                    "session_id": session_id,
                    "total_latency_ms": total_latency,
                    "error": str(e),
                    "session_creation_success": False
                }
            )
            raise HTTPException(status_code=503, detail="No available workers")


# Serve client.html at the root
@app.get("/")
async def get_client_html():
    return FileResponse("static/client.html")

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return JSONResponse({"status": "healthy"})

# Version endpoint
@app.get("/version")
async def get_version():
    """Get application version."""
    return JSONResponse({"version": __version__})

# Worker pool status endpoint
@app.get("/status/workers")
async def get_worker_status():
    """Get worker pool status and statistics."""
    try:
        stats = await session_manager.get_session_stats()
        return JSONResponse(stats)
    except Exception as e:
        logger.error(f"Failed to get worker status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get worker status")

# Model status endpoint  
@app.get("/status/models")
async def get_model_status():
    """Get shared model status and statistics."""
    try:
        stats = await shared_model_manager.get_model_stats()
        return JSONResponse(stats)
    except Exception as e:
        logger.error(f"Failed to get model status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get model status")

# Overall system status
@app.get("/status")
async def get_system_status():
    """Get overall system status."""
    try:
        worker_stats = await session_manager.get_session_stats()
        model_stats = await shared_model_manager.get_model_stats()
        
        # Test Redis connection
        redis_healthy = False
        try:
            await redis_manager.redis.ping()
            redis_healthy = True
        except:
            pass
        
        return JSONResponse({
            "status": "healthy" if redis_healthy and model_stats["loaded"] else "degraded",
            "redis_connected": redis_healthy,
            "models_loaded": model_stats["loaded"],
            "workers": worker_stats,
            "models": model_stats,
            "architecture": "worker_pool"
        })
    except Exception as e:
        logger.error(f"Failed to get system status: {e}")
        raise HTTPException(status_code=500, detail="Failed to get system status")

# Monitoring dashboard endpoint
@app.get("/dashboard")
async def get_monitoring_dashboard():
    """Get comprehensive monitoring dashboard data."""
    try:
        dashboard_data = await performance_monitor.get_dashboard_data()
        return JSONResponse(dashboard_data)
    except Exception as e:
        logger.error(f"Failed to get dashboard data: {e}")
        raise HTTPException(status_code=500, detail="Failed to get dashboard data")

# Performance metrics endpoint
@app.get("/metrics")
async def get_performance_metrics():
    """Get raw performance metrics."""
    try:
        metrics_summary = performance_monitor.metrics.get_summary()
        return JSONResponse(metrics_summary)
    except Exception as e:
        logger.error(f"Failed to get metrics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get metrics")

# Latency metrics endpoint
@app.get("/latency")
async def get_latency_metrics():
    """Get latency performance statistics."""
    try:
        # Get recent latency stats
        latency_stats = latency_tracker.get_performance_stats(time_window_minutes=60)
        return JSONResponse(latency_stats)
    except Exception as e:
        logger.error(f"Failed to get latency metrics: {e}")
        raise HTTPException(status_code=500, detail="Failed to get latency metrics")

# Session latency timeline endpoint
@app.get("/latency/session/{session_id}")
async def get_session_latency(session_id: str):
    """Get latency timeline for a specific session."""
    try:
        timeline = latency_tracker.get_session_timeline(session_id)
        summary = latency_tracker.get_session_summary(session_id)
        
        return JSONResponse({
            "session_id": session_id,
            "timeline": timeline,
            "summary": summary
        })
    except Exception as e:
        logger.error(f"Failed to get session latency for {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to get session latency")

# Debug endpoint for Redis state
@app.get("/debug/redis")
async def debug_redis():
    """Debug Redis state - workers and queues."""
    try:
        # Check Redis connection
        await redis_manager.redis.ping()
        
        # Get active workers
        active_workers = await redis_manager.get_active_workers()
        
        # Get queue lengths
        session_queue_len = await redis_manager.redis.llen("voice_sessions")
        response_queue_len = await redis_manager.redis.llen("worker_responses")
        
        # Get all worker keys
        worker_keys = await redis_manager.redis.keys("worker:*")
        
        # Get sample sessions from queue
        sample_sessions = await redis_manager.redis.lrange("voice_sessions", 0, 2)
        
        return JSONResponse({
            "redis_connected": True,
            "active_workers": active_workers,
            "worker_keys": worker_keys,
            "session_queue_length": session_queue_len,
            "response_queue_length": response_queue_len,
            "sample_sessions": sample_sessions
        })
    except Exception as e:
        logger.error(f"Failed to debug Redis: {e}")
        return JSONResponse({
            "redis_connected": False,
            "error": str(e)
        })

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
