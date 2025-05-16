import asyncio
import logging
import signal
import uvicorn
import os

from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Import necessary components from the new structure
from app.ws.live_session import handle_websocket_session, get_active_connections, get_shutdown_event
from app.core.config import logger # Use the logger from config

# Configure logging (if not already configured by config.py or elsewhere)
# logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# logger = logging.getLogger(__name__) # This is already done in config.py

app = FastAPI(title="Gemini Live Proxy Server")

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

# WebSocket endpoint
@app.websocket("/ws/live")
async def websocket_endpoint(websocket: WebSocket):
    await handle_websocket_session(websocket)

# Serve client.html at the root
@app.get("/")
async def get_client_html():
    return FileResponse("static/client.html")

# Health check endpoint
@app.get("/health")
async def health_check():
    logger.info("Health check endpoint called")
    return JSONResponse({"status": "healthy"})

# Graceful shutdown handling
async def shutdown_server():
    logger.info("Shutdown initiated, closing all connections...")
    shutdown_event = get_shutdown_event()
    shutdown_event.set()
    
    active_connections = get_active_connections()
    # Close all active WebSockets
    for ws in list(active_connections): # Iterate over a copy
        try:
            await ws.close(code=1001, reason="Server shutting down") # Send a specific close code
            if ws in active_connections: # Check again as it might have been removed
                active_connections.remove(ws)
            logger.info(f"Closed WebSocket connection: {ws.client}")
        except Exception as e:
            logger.error(f"Error closing websocket during shutdown: {e}")
    
    logger.info("All connections closed. Server shutdown complete.")

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup...")
    # You can add any startup logic here, e.g., initializing resources

@app.on_event("shutdown")
async def app_shutdown_event():
    logger.info("Application shutdown event triggered...")
    await shutdown_server()

# Signal handlers for graceful shutdown (uvicorn handles SIGINT/SIGTERM by default when run programmatically)
# However, if running directly with `python app/main.py` (not recommended for prod), these might be useful.
# For uvicorn, the @app.on_event("shutdown") is the primary mechanism.

def main():
    # This function is for running the server directly with `python app/main.py`
    # For production, use `uvicorn app.main:app --host 0.0.0.0 --port 8000`
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "0.0.0.0")
    
    logger.info(f"Starting server on {host}:{port}")
    
    # Setup signal handlers for direct script execution
    loop = asyncio.get_event_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(shutdown_server_signal(s, loop)))
        
    uvicorn.run(app, host=host, port=port, log_level="info")

async def shutdown_server_signal(sig, loop):
    logger.info(f"Received exit signal {sig.name}...")
    await shutdown_server()
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    [task.cancel() for task in tasks]
    logger.info(f"Cancelling {len(tasks)} outstanding tasks")
    await asyncio.gather(*tasks, return_exceptions=True)
    loop.stop()

if __name__ == "__main__":
    # Note: It's generally better to run FastAPI apps with Uvicorn directly,
    # e.g., `uvicorn app.main:app --reload` for development.
    # The main() function here is for convenience if you want to run `python app/main.py`.
    main()