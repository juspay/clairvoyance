import uvicorn
import json
import subprocess
import uuid
import time
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict
from enum import Enum

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
    # Initialize aiohttp session
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=DAILY_API_KEY,
        daily_api_url=DAILY_API_URL,
        aiohttp_session=aiohttp_session,
    )
    logger.info("Daily REST helper initialized.")
    
    yield
    
    logger.info("Application shutdown event triggered...")
    # Cleanup bot processes
    cleanup()
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


# WebSocket endpoint for Gemini Live
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await handle_websocket_session(websocket)

# Pipecat bot endpoint
class Mode(Enum):
    TEST = "test"
    LIVE = "live"

@app.post("/agent/voice/automatic")
async def bot_connect(request: Request) -> Dict[str, Any]:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        logger.error("Failed to decode JSON from request body")
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    logger.info(f"Received payload: {payload}")
    # 1. Extract and validate mode, defaulting to TEST on any error
    raw_mode = payload.get("mode", Mode.TEST.value)
    try:
        mode = Mode(raw_mode)
    except ValueError:
        # Invalid or missing → fallback to TEST
        mode = Mode.TEST

    euler_tok = payload.get("eulerToken")
    breeze_tok = payload.get("breezeToken")
    shop_url = payload.get("shopUrl")
    shop_id = payload.get("shopId")
    shop_type = payload.get("shopType")
    user_name = payload.get("userName")
    tts_service = payload.get("ttsService")

    # 2. Create room + token
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

    # 3. Generate unique session ID for this subprocess
    session_id = str(uuid.uuid4())
    logger.bind(session_id=session_id).info(f"Generated session ID for new voice agent: {session_id}")

    # 4. Build command args list
    bot_file = "app.agents.voice.automatic"
    cmd = [
        "python3", "-m", bot_file,
        "-u", room.url,
        "-t", token,
        "--mode", mode.value,
        "--session-id", session_id,
    ]

    # Add user_name and tts_service regardless of mode
    if user_name:
        cmd += ["--user-name", user_name]
    if tts_service:
        cmd += ["--tts-service", tts_service]

    # Only send external tokens when in LIVE mode
    if mode is Mode.LIVE:
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