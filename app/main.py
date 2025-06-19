import uvicorn
import os
import subprocess
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Dict

import aiohttp
from fastapi import FastAPI, WebSocket, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pipecat.transports.services.helpers.daily_rest import DailyRESTHelper, DailyRoomParams

# Import necessary components from the new structure
from app.ws.live_session import handle_websocket_session, get_active_connections, get_shutdown_event
from app.core.config import logger # Use the logger from config

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

# Store Daily API helpers
daily_helpers = {}


def cleanup():
    """Cleanup function to terminate all bot processes.

    Called during server shutdown.
    """
    for entry in bot_procs.values():
        proc = entry[0]
        proc.terminate()
        proc.wait()
    logger.info("All bot processes terminated.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan manager that handles startup and shutdown tasks."""
    logger.info("Application startup...")
    # Initialize aiohttp session
    aiohttp_session = aiohttp.ClientSession()
    daily_helpers["rest"] = DailyRESTHelper(
        daily_api_key=os.getenv("DAILY_API_KEY", ""),
        daily_api_url=os.getenv("DAILY_API_URL", "https://api.daily.co/v1"),
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


app = FastAPI(title="Breeze Automatic Server", lifespan=lifespan)

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
@app.post("/agent/voice/automatic")
async def bot_connect(request: Request) -> Dict[Any, Any]:
    """Connect endpoint that creates a room and returns connection credentials."""
    logger.info("Received request to connect pipecat bot.")
    try:
        room = await daily_helpers["rest"].create_room(DailyRoomParams())
        if not room.url:
            raise HTTPException(status_code=500, detail="Failed to create room")

        token = await daily_helpers["rest"].get_token(room.url)
        if not token:
            raise HTTPException(status_code=500, detail=f"Failed to get token for room: {room.url}")

        logger.info(f"Created Daily room: {room.url}")

        # Start the agent process
        bot_file = "app.agents.voice.automatic"
        command = f"python3 -m {bot_file} -u {room.url} -t {token}"
        
        logger.info(f"Launching agent process with command: {command}")
        
        proc = subprocess.Popen(
            [command],
            shell=True,
            bufsize=1,
            cwd=Path(__file__).parent.parent, # Run from project root
        )
        bot_procs[proc.pid] = (proc, room.url)
        logger.info(f"Bot process started with PID: {proc.pid}")

        return {"room_url": room.url, "token": token}

    except Exception as e:
        logger.error(f"Failed to start bot subprocess: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start subprocess: {e}")


# Serve client.html at the root
@app.get("/")
async def get_client_html():
    return FileResponse("static/client.html")

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return JSONResponse({"status": "healthy"})

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
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    uvicorn.run(app, host=host, port=port, log_level="info")