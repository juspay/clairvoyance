import asyncio
import json
import subprocess
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

import uvicorn
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.daily.utils import DailyRESTHelper

from app import __version__
from app.api.routers import automatic, breeze_buddy
from app.core.config import (
    DAILY_API_KEY,
    DAILY_API_URL,
    DAILY_ROOM_MAX_POOL_SIZE,
    DAILY_ROOM_POOL_SIZE,
    ENABLE_AUTOMATIC_DAILY_RECORDING,
    HOST,
    MAX_DAILY_SESSION_LIMIT,
    PORT,
    VOICE_AGENT_MAX_POOL_SIZE,
    VOICE_AGENT_POOL_SIZE,
)

# Import necessary components from the new structure
from app.core.logger import logger
from app.core.security.jwt import validate_automatic_request
from app.core.transport.http_client import create_aiohttp_session

# Database imports
from app.database import close_db_pool, get_db_connection, init_db_pool
from app.helpers.automatic.daily_room_pool import (
    cleanup_room_pool,
    get_room_pool,
    initialize_room_pool,
)
from app.helpers.automatic.process_pool import (
    cleanup_voice_agent_pool,
    get_voice_agent_pool,
    initialize_voice_agent_pool,
)
from app.helpers.automatic.session_manager import (
    bot_procs,
    cleanup_bot_processes,
    monitor_session_cleanup,
    session_cleanup_callback,
)
from app.schemas import (
    AutomaticVoiceUserConnectRequest,
)

# Store Daily API helpers and room pool
daily_helpers = {}


async def room_cleanup_callback(session_id: str):
    """Callback for room cleanup to avoid circular imports"""
    room_pool = get_room_pool()
    await room_pool.cleanup_and_replenish_room(session_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
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

    # Initialize Daily room pool
    try:
        await initialize_room_pool(
            daily_rest_helper=daily_helpers["rest"],
            pool_size=DAILY_ROOM_POOL_SIZE,
            max_pool_size=DAILY_ROOM_MAX_POOL_SIZE,
            max_session_limit=MAX_DAILY_SESSION_LIMIT,
            enable_recording=ENABLE_AUTOMATIC_DAILY_RECORDING,
        )
        logger.info("Daily room pool initialized")
    except Exception as e:
        logger.error(f"Failed to initialize room pool: {e}")

    # Initialize voice agent process pool
    try:
        await initialize_voice_agent_pool(
            pool_size=VOICE_AGENT_POOL_SIZE, max_pool_size=VOICE_AGENT_MAX_POOL_SIZE
        )

        # Set up callbacks to avoid circular imports
        pool = get_voice_agent_pool()
        pool.room_cleanup_callback = room_cleanup_callback
        pool.session_cleanup_callback = session_cleanup_callback

        logger.info("Voice agent process pool initialized with callbacks")

        # Start background task to monitor session cleanup
        asyncio.create_task(monitor_session_cleanup())
    except Exception as e:
        logger.error(f"Failed to initialize voice agent pool: {e}")

    yield

    logger.info("Application shutdown event triggered...")
    # Cleanup room pool
    await cleanup_room_pool()
    # Cleanup voice agent pool
    await cleanup_voice_agent_pool()
    # Cleanup bot processes
    await cleanup_bot_processes()
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
app.include_router(
    automatic.router, prefix="/agent/voice/automatic", tags=["Automatic Agent"]
)


# Pipecat bot endpoint
@app.post("/agent/voice/automatic")
async def bot_connect(
    request: AutomaticVoiceUserConnectRequest,
    user_context=Depends(validate_automatic_request),
) -> Dict[str, Any]:
    logger.info(
        f"Received new user connect request payload: {request.model_dump_json(exclude_none=True)}"
    )

    if user_context:
        logger.info(
            f"Authenticated user: {user_context['email']} (merchant: {user_context['merchantId']})"
        )

    # 1. Consolidate request parameters into a single dictionary
    session_params = {
        "mode": request.mode.upper() if request.mode else None,
        "user_name": request.userName,
        "user_email": request.email,
        "tts_provider": (
            request.ttsService.ttsProvider.value if request.ttsService else None
        ),
        "voice_name": (
            request.ttsService.voiceName.value if request.ttsService else None
        ),
        "euler_token": request.eulerToken,
        "breeze_token": request.breezeToken,
        "shop_url": request.shopUrl,
        "shop_id": request.shopId,
        "shop_type": request.shopType,
        "merchant_id": request.merchantId,
        "platform_integrations": request.platformIntegrations,
        "reseller_id": request.resellerId,
    }

    # 2. Get room from Daily room pool
    session_id = str(uuid.uuid4())
    room_pool = get_room_pool()
    daily_room = await room_pool.get_room(session_id)
    room_url = daily_room.room_url
    token = daily_room.user_token
    bot_token = daily_room.bot_token
    logger.info(f"Got room from pool for session {session_id}: {room_url}")

    # 3. Get client session ID
    client_sid = request.sessionId or str(uuid.uuid4())
    logger.bind(session_id=session_id).info(
        f"Using session ID for new voice agent: {session_id}"
    )
    logger.bind(client_sid=client_sid).info(
        f"Using client session ID for new voice agent: {client_sid}"
    )

    # Log the mapping between session_id and client_sid for easy reference
    logger.bind(session_id=session_id, client_sid=client_sid).info(
        "Voice agent session mapping created"
    )

    # 4. Try to get process from pool
    pool = get_voice_agent_pool()
    try:
        voice_process = await pool.get_process(session_id)

        try:
            # Configure the pre-warmed process
            session_config = {
                "room_url": room_url,
                "token": bot_token,
                "session_id": session_id,
                "client_sid": client_sid,
                **session_params,
            }

            config_json = json.dumps(session_config) + "\n"
            voice_process.process.stdin.write(config_json.encode("utf-8"))
            await voice_process.process.stdin.drain()

            logger.bind(session_id=session_id).info(
                f"Assigned pre-warmed process {voice_process.process_id} to session {session_id}"
            )

            bot_procs[voice_process.process.pid] = (
                voice_process.process,
                room_url,
                session_id,
                "pool",
            )

            return {"room_url": room_url, "token": token, "session_id": session_id}

        except Exception as write_error:
            logger.error(
                f"Failed to configure pooled process {voice_process.process_id}, returning to pool: {write_error}"
            )
            # Return the process to the pool to prevent a leak
            await pool.return_process(session_id)
            # Re-raise to trigger the fallback mechanism
            raise

    except Exception as e:
        logger.warning(
            f"Failed to get process from pool: {e}, falling back to direct creation"
        )

        # 5. Fallback: Launch subprocess directly
        bot_file = "app.agents.voice.automatic"
        cmd = [
            "python3",
            "-m",
            bot_file,
            "-u",
            room_url,
            "-t",
            bot_token,
            "--session-id",
            session_id,
            "--client-sid",
            client_sid,
        ]

        # Dynamically build command arguments from session_params
        arg_map = {
            "mode": "--mode",
            "user_name": "--user-name",
            "user_email": "--user-email",
            "tts_provider": "--tts-provider",
            "voice_name": "--voice-name",
            "euler_token": "--euler-token",
            "breeze_token": "--breeze-token",
            "shop_url": "--shop-url",
            "shop_id": "--shop-id",
            "shop_type": "--shop-type",
            "merchant_id": "--merchant-id",
            "platform_integrations": "--platform-integrations",
            "reseller_id": "--reseller-id",
        }

        for key, value in session_params.items():
            if value is not None:
                arg_name = arg_map.get(key)
                if isinstance(value, list):
                    cmd.extend([arg_name] + value)
                else:
                    cmd.extend([arg_name, str(value)])

        logger.bind(session_id=session_id).info(
            f"Launching subprocess with command: {' '.join(cmd)}"
        )
        proc = subprocess.Popen(
            cmd,
            cwd=Path(__file__).parent.parent,
            bufsize=1,
        )
        bot_procs[proc.pid] = (proc, room_url, session_id, "direct")
        logger.bind(session_id=session_id).info(
            f"Subprocess started with PID: {proc.pid}"
        )

        return {"room_url": room_url, "token": token, "session_id": session_id}


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
