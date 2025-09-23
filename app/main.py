import subprocess
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.daily.utils import (
    DailyMeetingTokenParams,
    DailyMeetingTokenProperties,
    DailyRESTHelper,
    DailyRoomParams,
    DailyRoomProperties,
)

from app import __version__
from app.api.routers import breeze_buddy
from app.core.config import (
    DAILY_API_KEY,
    DAILY_API_URL,
    ENABLE_AUTOMATIC_DAILY_RECORDING,
    HOST,
    MAX_DAILY_SESSION_LIMIT,
    PORT,
    ROOM_POOL_ENABLED,
    ROOM_POOL_GRADUAL_ROLLOUT,
    ROOM_POOL_ROLLOUT_PERCENTAGE,
)

# Import necessary components from the new structure
from app.core.logger import logger
from app.core.transport.http_client import create_aiohttp_session

# Database imports
from app.database import close_db_pool, get_db_connection, init_db_pool
from app.schemas import (
    AutomaticVoiceUserConnectRequest,
)

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

# Store Daily API helpers
daily_helpers = {}

# Global room pool instance
room_pool = None


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
                logger.info(
                    f"Process {pid} for room {room_url} has already terminated."
                )
        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}", exc_info=True)
        finally:
            # Ensure the process is removed from the tracking dictionary
            bot_procs.pop(pid, None)
    logger.info("All bot processes have been handled.")


async def _get_room_and_token_simple(session_id: str, client_session_id: str, metadata: Dict[str, Any]) -> tuple[str, str]:
    """Simple room+token retrieval from pool or fallback"""
    global room_pool

    if room_pool and ROOM_POOL_ENABLED:
        try:
            return await room_pool.get_room_and_token(session_id)
        except Exception as e:
            logger.error(f"Pool failed for {session_id}: {e}")
            # Fall through to direct creation

    # Direct creation (original fallback logic)
    return await _create_room_and_token_direct(session_id, client_session_id, metadata)


async def _create_room_and_token_direct(session_id: str, client_session_id: str = None, metadata: Dict[str, Any] = None) -> tuple[str, str]:
    """Direct room+token creation (fallback)"""
    logger.info(f"Creating room and token directly for session {session_id}")

    try:
        # Create room
        room_params = DailyRoomParams(
            properties=DailyRoomProperties(
                exp=time.time() + MAX_DAILY_SESSION_LIMIT,
                eject_at_room_exp=True,
                enable_recording="cloud" if ENABLE_AUTOMATIC_DAILY_RECORDING else None,
            )
        )

        room = await daily_helpers["rest"].create_room(params=room_params)

        # Generate token with proper structure
        token_params = DailyMeetingTokenParams(
            properties=DailyMeetingTokenProperties(
                user_id=session_id,
                user_name=client_session_id or session_id,
                eject_after_elapsed=MAX_DAILY_SESSION_LIMIT,
                is_owner=True,
                # Note: Custom metadata removed from permissions to avoid Daily.co API errors
                # session_id, client_id are tracked via user_id and user_name fields
            )
        )

        token = await daily_helpers["rest"].get_token(
            room.url,
            expiry_time=MAX_DAILY_SESSION_LIMIT,
            eject_at_token_exp=True,
            owner=True,
            params=token_params
        )

        logger.bind(session_id=session_id, room_url=room.url).info("Room and token created directly")
        return room.url, token

    except Exception as e:
        logger.error(f"Failed to create room and token for session {session_id}: {e}")
        raise Exception(f"Unable to create Daily.co room and token: {str(e)}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    global room_pool

    logger.info("" \
    "...")

    # Initialize database and create tables if needed
    try:
        await init_db_pool()
    except Exception as e:
        logger.error(f"Failed to initialize database: {e}")

    # Initialize aiohttp session with proxy support for Daily API
    aiohttp_session = create_aiohttp_session()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=DAILY_API_KEY,
        daily_api_url=DAILY_API_URL,
        aiohttp_session=aiohttp_session,
    )
    logger.info("Daily REST helper initialized with proxy support.")

    # Initialize simplified room pool if enabled
    if ROOM_POOL_ENABLED:
        try:
            from app.services.daily_room_pool import SimpleDailyRoomPool
            room_pool = SimpleDailyRoomPool(daily_helpers["rest"])
            await room_pool.start()
            logger.info("Simplified room pool service started successfully")
        except Exception as e:
            logger.error(f"Failed to start room pool service: {e}")
            logger.warning("Continuing without room pool - will use direct room creation")
            room_pool = None
    else:
        logger.info("Room pool service disabled via configuration")

    yield

    logger.info("Application shutdown event triggered...")

    # Stop room pool service
    if room_pool:
        try:
            await room_pool.stop()
            logger.info("Room pool service stopped")
        except Exception as e:
            logger.error(f"Error stopping room pool service: {e}")

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

app.include_router(
    breeze_buddy.router, prefix="/agent/voice/breeze-buddy", tags=["Breeze Buddy"]
)


# Pipecat bot endpoint
@app.post("/agent/voice/automatic")
async def bot_connect(request: AutomaticVoiceUserConnectRequest) -> Dict[str, Any]:
    logger.info(
        f"Received new user connect request payload: {request.model_dump_json(exclude_none=True)}"
    )
    # 1. Validate request
    raw_mode = request.mode
    euler_tok = request.eulerToken
    breeze_tok = request.breezeToken
    shop_url = request.shopUrl
    shop_id = request.shopId
    shop_type = request.shopType
    user_email = request.email
    user_name = request.userName
    tts_provider = request.ttsService.ttsProvider.value if request.ttsService else None
    voice_name = request.ttsService.voiceName.value if request.ttsService else None
    merchant_id = request.merchantId
    platform_integrations = request.platformIntegrations
    reseller_id = request.resellerId

    # 2. Generate unique session ID and client session ID for this subprocess
    session_id = str(uuid.uuid4())  # Always generate random session ID
    client_sid = request.sessionId or str(
        uuid.uuid4()
    )  # Use client-provided sessionId or generate fallback
    logger.bind(session_id=session_id).info(
        f"Generated session ID for new voice agent: {session_id}"
    )
    logger.bind(client_sid=client_sid).info(
        f"Using client session ID for new voice agent: {client_sid}"
    )

    # 3. Get room and token (using simplified pool or fallback)
    room_url, token = await _get_room_and_token_simple(session_id, client_sid, {
        "user_email": user_email,
        "user_name": user_name,
        "shop_id": shop_id,
        "shop_type": shop_type,
        "merchant_id": merchant_id,
        "mode": raw_mode,
        "reseller_id": reseller_id,
    })

    # 4. Build command args list
    bot_file = "app.agents.voice.automatic"
    cmd = [
        "python3",
        "-m",
        bot_file,
        "-u",
        room_url,
        "-t",
        token,
        "--mode",
        raw_mode.upper() if raw_mode else None,
        "--session-id",
        session_id,
        "--client-sid",
        client_sid,
    ]

    # Add user_name and tts_service regardless of mode
    if user_name:
        cmd += ["--user-name", user_name]
    if user_email:
        cmd += ["--user-email", user_email]
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

    if reseller_id:
        cmd += ["--reseller-id", reseller_id]

    # 5. Launch subprocess without shell
    logger.bind(session_id=session_id).info(
        f"Launching subprocess with command: {' '.join(cmd)}"
    )
    proc = subprocess.Popen(
        cmd,
        cwd=Path(__file__).parent.parent,
        bufsize=1,
    )
    bot_procs[proc.pid] = (proc, room_url)
    logger.bind(session_id=session_id).info(f"Subprocess started with PID: {proc.pid}")

    return {"room_url": room_url, "token": token}


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
                return JSONResponse(
                    {
                        "status": "healthy",
                        "database": "connected",
                        "message": "Database connection is healthy",
                    }
                )
            else:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "unhealthy",
                        "database": "error",
                        "message": "Database query returned unexpected result",
                    },
                )
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return JSONResponse(
            status_code=400,
            content={
                "status": "unhealthy",
                "database": "disconnected",
                "message": f"Database connection failed: {str(e)}",
            },
        )


# Version endpoint
@app.get("/version")
async def get_version():
    """Get application version."""
    return JSONResponse({"version": __version__})


# Simplified room pool health check endpoints
@app.get("/health/room-pool")
async def simple_room_pool_health():
    """Simple room pool health check"""
    if not ROOM_POOL_ENABLED or not room_pool:
        return JSONResponse({
            "status": "disabled",
            "pool_enabled": False
        })

    try:
        stats = room_pool.get_stats()
        current_size = room_pool.ready_rooms.qsize()

        status = stats.health_status
        warnings = []

        if current_size == 0:
            warnings.append("No rooms available in pool")
        elif current_size < 3:
            warnings.append("Low room availability")

        return JSONResponse({
            "status": status,
            "pool_size": current_size,
            "target_size": room_pool.config.target_pool_size,
            "pool_hit_rate_pct": round(stats.pool_hit_rate, 2),
            "stats": {
                "rooms_created": stats.rooms_created,
                "rooms_served": stats.rooms_served,
                "fallback_used": stats.fallback_used,
                "creation_errors": stats.creation_errors,
                "expired_cleaned": stats.expired_cleaned,
                "uptime_hours": round(stats.uptime_hours, 2)
            },
            "warnings": warnings,
            "timestamp": time.time()
        })

    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "message": str(e)}
        )


@app.get("/health/room-pool/metrics")
async def simple_room_pool_metrics():
    """Get basic room pool metrics"""
    if not ROOM_POOL_ENABLED or not room_pool:
        return JSONResponse(
            status_code=404,
            content={"error": "Room pool service not available"}
        )

    try:
        stats = room_pool.get_stats()
        metrics_dict = room_pool.metrics.get_stats_dict()

        return JSONResponse({
            "pool_stats": {
                "current_size": room_pool.ready_rooms.qsize(),
                "target_size": room_pool.config.target_pool_size,
                "health_status": stats.health_status,
            },
            "performance": {
                "pool_hit_rate_pct": round(stats.pool_hit_rate, 2),
                "uptime_hours": round(stats.uptime_hours, 2),
            },
            "counters": metrics_dict,
            "timestamp": time.time()
        })

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"Failed to retrieve metrics: {str(e)}"}
        )


@app.get("/health/room-pool/rollout")
async def simple_room_pool_rollout():
    """Get simplified room pool rollout status"""
    if not ROOM_POOL_ENABLED:
        return JSONResponse({
            "status": "disabled",
            "rollout_enabled": False
        })

    return JSONResponse({
        "status": "enabled" if ROOM_POOL_ENABLED else "disabled",
        "rollout_enabled": ROOM_POOL_GRADUAL_ROLLOUT,
        "rollout_percentage": ROOM_POOL_ROLLOUT_PERCENTAGE,
        "effective_percentage": ROOM_POOL_ROLLOUT_PERCENTAGE if ROOM_POOL_GRADUAL_ROLLOUT else 100
    })


# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
