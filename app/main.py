import uvicorn
import json
import subprocess
import uuid
import time
import asyncio
from pathlib import Path
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

# Dictionary to track bot processes: {pid: (process, room_url)}
bot_procs = {}

# Store Daily API helpers
daily_helpers = {}

# Queue for handling sequential call processing
call_queue = asyncio.Queue()
call_in_progress = asyncio.Event()


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


async def process_call_queue():
    """Processes the call queue sequentially."""
    logger.info("Call queue processor started.")
    while True:
        # Wait for an item in the queue
        order_details = await call_queue.get()
        
        # Signal that a call is about to start
        call_in_progress.clear()
        logger.info(f"Processing call for order: {order_details['order'].order_id}")
        
        try:
            await make_twilio_call(order_details['identity'], order_details['order'])
            # Wait for the call to complete (or for a timeout)
            await asyncio.wait_for(call_in_progress.wait(), timeout=300)  # 5-minute timeout
        except asyncio.TimeoutError:
            logger.error("Timeout waiting for call completion signal.")
        except Exception as e:
            logger.error(f"An error occurred while processing the call: {e}")
        finally:
            # Mark the task as done
            call_queue.task_done()
            logger.info("Call processing finished, ready for next.")


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
    
    # Start the background task to process the call queue
    asyncio.create_task(process_call_queue())
    
    yield
    
    logger.info("Application shutdown event triggered...")
    # Cleanup bot processes
    cleanup()
    # Close aiohttp session
    await aiohttp_session.close()
    logger.info("Aiohttp session closed.")
    # Gracefully shutdown websocket connections
    await shutdown_server()
    # Signal that no call is in progress on shutdown
    call_in_progress.set()


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

async def make_twilio_call(identity: str, order: BreezeOrderData):
    """
    Helper function to create a Twilio call.
    """
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
        call_in_progress.set()  # Signal completion to unblock queue
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/agent/voice/breeze-buddy/{identity}/order-confirmation")
async def trigger_order_confirmation(identity: str, order: BreezeOrderData):
    """
    Receives order details and adds them to a queue for processing.
    """
    if identity != "breeze":
        raise HTTPException(status_code=404, detail="Feature not supported")
    
    logger.info(f"Queuing order: {order.order_id} for {order.customer_name}")

    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER]):
        raise HTTPException(status_code=500, detail="Twilio credentials are not configured.")

    await call_queue.put({"identity": identity, "order": order})
    
    return {"status": "queued", "order_id": order.order_id}


@app.websocket("/agent/voice/breeze-buddy/{serviceIdentifier}/callback/{workflow}")
async def telephony_websocket_handler(serviceIdentifier: str, workflow: str, websocket: WebSocket):
    """
    WebSocket endpoint that accepts a connection and passes it to the
    pipecat bot's main function.
    """
    
    if serviceIdentifier != "twillio" or workflow != "order-confirmation":
        raise HTTPException(status_code=404, detail="Feature not supported for this service or workflow")
    
    try:
        # The websocket_bot_main function handles the entire
        # lifecycle of the WebSocket connection, including accept().
        await telephony_websocket_conn(websocket, aiohttp.ClientSession())
    except WebSocketDisconnect:
        logger.warning("WebSocket client disconnected.")
    except Exception as e:
        logger.error(f"An error occurred in the WebSocket handler: {e}")
        await websocket.close(code=1011, reason="Internal Server Error")
    finally:
        logger.info("WebSocket client connection closed, signaling call completion.")
        call_in_progress.set()


# @app.post("/order/confirmation/webhook/call-summary")
# async def call_summary_webhook(request: Request):
#     summary = await request.json()
#     call_sid = summary.get("call_sid")
#     outcome = summary.get("outcome")
#     transcription = summary.get("transcription")

#     if call_sid:
#         logger.info(f"Received call summary for {call_sid}")
#     if outcome:
#         logger.info(f"Outcome: {outcome}")
#     if transcription:
#         logger.info(f"Transcription: {transcription}")
        
#     return {"status": "received"}


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
