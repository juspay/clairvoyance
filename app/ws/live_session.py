import asyncio
import json
import logging
import time
import traceback
from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types

from app.core.config import PING_INTERVAL, FRAME_SIZE, SAMPLE_RATE
from app.services.gemini_service import create_gemini_session, close_gemini_session, process_tool_calls

logger = logging.getLogger(__name__)

active_connections = set()
shutdown_event = asyncio.Event() # This might be better managed at the app level

async def handle_websocket_session(websocket: WebSocket):
    session_id = f"session_{len(active_connections) + 1}_{int(time.time())}"
    token = websocket.query_params.get("token")
    testmode_param = websocket.query_params.get("testmode", "false").lower()
    is_test_mode = testmode_param == "true"

    if not token:
        logger.error(f"[{session_id}] Missing Juspay token in WebSocket connection")
        await websocket.close(code=4001, reason="Missing Juspay token")
        return

    await websocket.accept()
    logger.info(f"[{session_id}] WebSocket connection established. Token received.")
    active_connections.add(websocket)
    
    # Store token and session_id in websocket.state for access in other parts (like tool calls)
    websocket.state.juspay_token = token
    websocket.state.session_id = session_id
    
    last_heartbeat = time.time()
    gemini_session = None
    gemini_session_cm = None
    websocket_active = True
    user_turn_started = False
    model_turn_started = False

    async def keepalive():
        nonlocal last_heartbeat
        while websocket_active and not shutdown_event.is_set():
            try:
                if time.time() - last_heartbeat > PING_INTERVAL:
                    try:
                        await websocket.send_text(json.dumps({"type": "ping"}))
                        last_heartbeat = time.time()
                    except Exception:
                        break 
                await asyncio.sleep(1)
            except Exception as e:
                logger.debug(f"[{session_id}] Keepalive ping failed: {e}")
                break

    try:
        logger.info(f"[{session_id}] Test mode active: {is_test_mode}")
        gemini_session, gemini_session_cm = await create_gemini_session(test_mode=is_test_mode)
    except Exception as e:
        logger.error(f"[{session_id}] Failed to establish Gemini session: {e}")
        await websocket.send_text(json.dumps({"type": "error", "message": "Failed to connect to Gemini"}))
        if websocket in active_connections: active_connections.remove(websocket)
        await websocket.close()
        return

    async def receive_from_client():
        nonlocal last_heartbeat, websocket_active, user_turn_started
        try:
            while websocket_active and not shutdown_event.is_set():
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=1.0)
                    last_heartbeat = time.time()

                    if message.get("type") == "websocket.receive":
                        if "text" in message:
                            data = json.loads(message["text"])
                            if data.get("type") == "pong":
                                logger.debug(f"[{session_id}] Received pong")
                                continue
                            elif data.get("type") == "ping":
                                await websocket.send_text(json.dumps({"type": "pong"}))
                                logger.debug(f"[{session_id}] Received ping, sent pong")
                                continue
                        
                        if "bytes" in message:
                            audio_data = message["bytes"]
                            if len(audio_data) != FRAME_SIZE:
                                logger.warning(f"[{session_id}] Received data with unexpected size: {len(audio_data)} bytes (expected {FRAME_SIZE})")
                                continue

                            if gemini_session and not shutdown_event.is_set():
                                try:
                                    await gemini_session.send_realtime_input(
                                        audio=types.Blob(data=audio_data, mime_type=f"audio/pcm;rate={SAMPLE_RATE}")
                                    )
                                except Exception as e:
                                    logger.error(f"[{session_id}] Error sending audio to Gemini: {e}")
                                    if "closed" in str(e).lower():
                                        websocket_active = False
                                        break
                except asyncio.TimeoutError:
                    continue
                except WebSocketDisconnect:
                    logger.info(f"[{session_id}] WebSocket disconnected in receive_from_client")
                    websocket_active = False
                    break
                except Exception as e:
                    if "disconnect message has been received" in str(e):
                        logger.info(f"[{session_id}] WebSocket disconnect detected in receive_from_client")
                        websocket_active = False
                        break
                    else:
                        logger.error(f"[{session_id}] Error processing client message: {e}")
                        logger.debug(traceback.format_exc())
        except Exception as e:
            logger.error(f"[{session_id}] Error in receive_from_client: {e}")
            logger.debug(traceback.format_exc())
            websocket_active = False

    async def forward_from_gemini():
        nonlocal websocket_active, model_turn_started, user_turn_started
        try:
            while not shutdown_event.is_set() and websocket_active and gemini_session:
                try:
                    async for resp in gemini_session.receive():
                        if not websocket_active or shutdown_event.is_set():
                            break
                        try:
                            # Handle automatic VAD events
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'activity_detected'):
                                activity = resp.server_content.activity_detected
                                if activity:
                                    logger.info(f"[{session_id}] User speech activity detected by automatic VAD")
                                    if not user_turn_started: # Send only if not already started
                                        user_turn_started = True
                                        model_turn_started = False
                                        await websocket.send_text(json.dumps({"type": "turn_start", "role": "user"}))
                            
                            # Handle turn determination
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'model_turn'):
                                if resp.server_content.model_turn and not model_turn_started:
                                    logger.info(f"[{session_id}] Model turn detected")
                                    model_turn_started = True
                                    user_turn_started = False
                                    await websocket.send_text(json.dumps({"type": "turn_start", "role": "model"}))

                            text_content = ""
                            if hasattr(resp, 'parts'):
                                for part in resp.parts:
                                    if hasattr(part, 'text') and part.text:
                                        text_content += part.text
                                if text_content:
                                    logger.info(f"[{session_id}] Received text response from Gemini: {text_content[:30]}...")
                                    await websocket.send_text(json.dumps({"type": "llm_transcript", "text": text_content}))
                            
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'input_transcription'):
                                input_transcription = resp.server_content.input_transcription
                                if hasattr(input_transcription, 'text') and input_transcription.text:
                                    logger.debug(f"[{session_id}] Received input audio transcription: {input_transcription.text[:30]}...")
                                    await websocket.send_text(json.dumps({"type": "input_transcript", "text": input_transcription.text}))
                                    if not user_turn_started: # Ensure user turn is marked
                                        user_turn_started = True
                                        model_turn_started = False # Reset model turn if user speaks
                                        await websocket.send_text(json.dumps({"type": "turn_start", "role": "user"}))

                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'output_transcription'):
                                output_transcription = resp.server_content.output_transcription
                                if hasattr(output_transcription, 'text') and output_transcription.text:
                                    logger.debug(f"[{session_id}] Received output audio transcription: {output_transcription.text[:30]}...")
                                    await websocket.send_text(json.dumps({"type": "audio_transcript", "text": output_transcription.text}))
                                    if not model_turn_started: # Ensure model turn is marked
                                        model_turn_started = True
                                        user_turn_started = False # Reset user turn if model speaks
                                        await websocket.send_text(json.dumps({"type": "turn_start", "role": "model"}))
                            
                            if hasattr(resp, 'server_content') and hasattr(resp.server_content, 'interrupted'):
                                if resp.server_content.interrupted:
                                    logger.info(f"[{session_id}] Model was interrupted by user")
                                    await websocket.send_text(json.dumps({"type": "interrupted"}))

                            if hasattr(resp, 'parts'):
                                for part in resp.parts:
                                    if hasattr(part, 'inline_data') and part.inline_data and part.inline_data.mime_type.startswith('audio/'):
                                        audio_data = part.inline_data.data
                                        logger.debug(f"[{session_id}] Received audio data from Gemini: {len(audio_data)} bytes") # Changed to DEBUG
                                        await websocket.send_bytes(b"\x01" + audio_data) # Marker byte for client
                                    # Other part types (executable_code, etc.) can be handled here if needed

                            elif hasattr(resp, 'data') and resp.data: # Fallback for direct audio
                                logger.debug(f"[{session_id}] Received audio data from Gemini via resp.data: {len(resp.data)} bytes") # Changed to DEBUG
                                await websocket.send_bytes(b"\x01" + resp.data)

                            if hasattr(resp, 'tool_call') and resp.tool_call is not None:
                                logger.info(f"[{session_id}] Received tool_call from Gemini: {resp.tool_call}")
                                # Pass websocket.state which contains juspay_token and session_id
                                function_responses = await process_tool_calls(resp.tool_call, websocket.state)
                                logger.info(f"[{session_id}] Processed function responses: {function_responses}")
                                if function_responses and gemini_session:
                                    await gemini_session.send_tool_response(function_responses=function_responses)
                        
                        except WebSocketDisconnect:
                            logger.info(f"[{session_id}] WebSocket disconnected in forward_from_gemini (inner)")
                            websocket_active = False
                            break
                        except Exception as e:
                            if "disconnect message has been received" in str(e) or "Connection closed" in str(e):
                                logger.info(f"[{session_id}] WebSocket connection closed: {e}")
                                websocket_active = False
                                break
                            else:
                                logger.error(f"[{session_id}] Error sending response to client: {e}")
                                logger.debug(traceback.format_exc())
                except asyncio.CancelledError:
                    logger.info(f"[{session_id}] Forward task cancelled")
                    break
                except Exception as e:
                    if "closed session" in str(e).lower():
                        logger.info(f"[{session_id}] Gemini session closed")
                        break
                    else:
                        logger.error(f"[{session_id}] Error in Gemini response handling: {e}")
                        logger.debug(traceback.format_exc())
                        await asyncio.sleep(0.1) # Avoid tight loop
        except Exception as e:
            logger.error(f"[{session_id}] Error in forward_from_gemini (outer): {e}")
            logger.debug(traceback.format_exc())
            websocket_active = False

    tasks = []
    try:
        tasks = [
            asyncio.create_task(keepalive()),
            asyncio.create_task(receive_from_client()),
            asyncio.create_task(forward_from_gemini())
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True) # Allow pending tasks to finish cancelling
    finally:
        for task in tasks: # Ensure all tasks are cancelled
            if not task.done():
                task.cancel()
        
        await close_gemini_session(gemini_session_cm)
        
        if websocket in active_connections:
            active_connections.remove(websocket)
        
        try:
            if websocket.client_state != WebSocketDisconnect: # Check if not already closed
                 await websocket.close()
        except Exception:
            pass # Ignore errors during close, it might already be closed
        logger.info(f"[{session_id}] WebSocket connection closed and resources cleaned up.")

# Functions to be called by main.py for app lifecycle
def get_active_connections():
    return active_connections

def get_shutdown_event():
    return shutdown_event