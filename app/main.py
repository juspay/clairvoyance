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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    logger.info("Application startup...")

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
    client_sid = request.sessionId or str(
        uuid.uuid4()
    )  # Use client-provided sessionId or generate fallback
    logger.bind(session_id=session_id).info(
        f"Generated session ID for new voice agent: {session_id}"
    )
    logger.bind(client_sid=client_sid).info(
        f"Using client session ID for new voice agent: {client_sid}"
    )

    # 4. Build command args list
    bot_file = "app.agents.voice.automatic"
    cmd = [
        "python3",
        "-m",
        bot_file,
        "-u",
        room.url,
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


# The main block is now only for direct execution, which is not the recommended way.
# Uvicorn running from run.py is the standard.
if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
