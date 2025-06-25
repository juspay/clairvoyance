import asyncio
import json
import time
import traceback
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from google.genai import types
from datetime import datetime as dt, time as dt_time, timezone as dt_timezone, timedelta
import pytz

from app.core.logger import logger
from app.core.config import PING_INTERVAL, FRAME_SIZE, SAMPLE_RATE
from app.services.gemini_service import create_gemini_session, close_gemini_session, process_tool_calls
from app.api.auth import validate_euler_auth, fetch_breeze_token, ValidateEulerAuthStatus, FetchTokenStatus
from app.api.juspay_metrics import (
    get_cumulative_juspay_analytics,
    JuspayAPIError
)
from app.api.shops import fetch_shop_data, Shop
from app.api.breeze_metrics import get_breeze_analytics, BreezeAnalyticsError
from app.data.dummy.analytics_data import dummy_juspay_analytics_today, dummy_breeze_analytics_today, dummy_juspay_analytics_weekly, dummy_breeze_analytics_weekly

active_connections = set()
shutdown_event = asyncio.Event() # This might be better managed at the app level


async def _perform_pre_gemini_calls(token: str, session_id: str):
    """
    Performs a series of API calls before Gemini initialization for non-test mode.
    Logs results and handles errors gracefully.
    Returns a dictionary with stringified analytics data and current timestamp.
    """
    merchant_id_found: str | None = None
    actual_breeze_token: str | None = None
    shop_details_list: list[Shop] | None = None
    
    # Initialize return values
    juspay_analytics_today_str: Optional[str] = None
    breeze_analytics_today_str: Optional[str] = None
    juspay_analytics_weekly_str: Optional[str] = None
    breeze_analytics_weekly_str: Optional[str] = None
    current_kolkata_time_str: Optional[str] = None

    # Step 1: Validate Euler Auth
    try:
        euler_auth_result = await validate_euler_auth(token=token)
        if euler_auth_result.status == ValidateEulerAuthStatus.SUCCESS:
            merchant_id_found = euler_auth_result.merchant_id
        else:
            logger.error(f"[{session_id}] Euler auth failed: {euler_auth_result.status} - {getattr(euler_auth_result, 'message', 'No message')}")
    except Exception as e:
        logger.error(f"[{session_id}] Exception during Euler auth validation: {e}", exc_info=True)

    # Step 2: Define Time Ranges and Fetch Analytics
    try:
        ist_timezone = pytz.timezone("Asia/Kolkata")
        now_ist = dt.now(ist_timezone)
        current_kolkata_time_str = now_ist.strftime('%Y-%m-%d %H:%M:%S %Z%z')

        # Define time ranges
        end_time_utc = now_ist.astimezone(dt_timezone.utc)
        end_time_iso_str = end_time_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Today's range
        start_of_today_ist = ist_timezone.localize(dt.combine(now_ist.date(), dt_time.min))
        start_of_today_utc = start_of_today_ist.astimezone(dt_timezone.utc)
        start_today_iso_str = start_of_today_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        # Weekly range (last 7 days)
        start_of_week_ist = ist_timezone.localize(dt.combine(now_ist.date() - timedelta(days=7), dt_time.min))
        start_of_week_utc = start_of_week_ist.astimezone(dt_timezone.utc)
        start_week_iso_str = start_of_week_utc.strftime('%Y-%m-%dT%H:%M:%SZ')

        # --- Fetch Today's Analytics ---
        logger.info(f"[{session_id}] Fetching TODAY's analytics data...")
        juspay_today_obj = await get_cumulative_juspay_analytics(
            login_token=token, start_time_iso=start_today_iso_str, end_time_iso=end_time_iso_str
        )
        if juspay_today_obj:
            juspay_analytics_today_str = juspay_today_obj.model_dump_json(indent=2)
            logger.info(f"[{session_id}] Full Cumulative Juspay Analytics Data (Today):\n{juspay_analytics_today_str}")
            if juspay_today_obj.errors:
                logger.error(f"[{session_id}] Errors during Juspay analytics fetching (Today): {juspay_today_obj.errors}")
        else:
            juspay_analytics_today_str = "{}"
            logger.error(f"[{session_id}] get_cumulative_juspay_analytics returned None or empty for Today's data.")

        # --- Fetch Weekly Analytics ---
        logger.info(f"[{session_id}] Fetching WEEKLY analytics data...")
        juspay_weekly_obj = await get_cumulative_juspay_analytics(
            login_token=token, start_time_iso=start_week_iso_str, end_time_iso=end_time_iso_str
        )
        if juspay_weekly_obj:
            juspay_analytics_weekly_str = juspay_weekly_obj.model_dump_json(indent=2)
            logger.info(f"[{session_id}] Full Cumulative Juspay Analytics Data (Weekly):\n{juspay_analytics_weekly_str}")
            if juspay_weekly_obj.errors:
                logger.error(f"[{session_id}] Errors during Juspay analytics fetching (Weekly): {juspay_weekly_obj.errors}")
        else:
            juspay_analytics_weekly_str = "{}"
            logger.error(f"[{session_id}] get_cumulative_juspay_analytics returned None or empty for Weekly data.")

    except JuspayAPIError as e:
        logger.error(f"[{session_id}] JuspayAPIError during analytics fetch: {e}")
    except ValueError as e:
        logger.error(f"[{session_id}] ValueError during analytics fetch: {e}")
    except Exception as e:
        logger.error(f"[{session_id}] Unexpected error during analytics fetch: {e}", exc_info=True)

    # Step 3: Fetch Shop Data
    if merchant_id_found:
        try:
            shop_response_obj = await fetch_shop_data(merchant_id=merchant_id_found)
            if shop_response_obj and shop_response_obj.shops:
                shop_details_list = shop_response_obj.shops
        except Exception as e:
            logger.error(f"[{session_id}] Exception during shop data fetching: {e}", exc_info=True)

    # Step 4: Fetch Breeze Token
    try:
        breeze_token_result = await fetch_breeze_token(platform_token=token)
        if breeze_token_result.status == FetchTokenStatus.SUCCESS and hasattr(breeze_token_result, 'token'):
            actual_breeze_token = breeze_token_result.token
    except Exception as e:
        logger.error(f"[{session_id}] Exception during Breeze token fetching: {e}", exc_info=True)

    # Step 5: Fetch Breeze Analytics (Today and Weekly)
    if actual_breeze_token and shop_details_list and len(shop_details_list) > 0:
        first_shop = shop_details_list[0]
        try:
            # Fetch Today's Breeze Analytics
            logger.info(f"[{session_id}] Fetching TODAY's Breeze analytics...")
            breeze_today_raw = await get_breeze_analytics(
                breeze_token=actual_breeze_token, start_time_iso=start_today_iso_str, end_time_iso=end_time_iso_str,
                shop_id=first_shop.id, shop_url=first_shop.url, shop_type=first_shop.type
            )
            breeze_analytics_today_str = json.dumps(breeze_today_raw, indent=2) if breeze_today_raw else "{}"

            # Fetch Weekly Breeze Analytics
            logger.info(f"[{session_id}] Fetching WEEKLY Breeze analytics...")
            breeze_weekly_raw = await get_breeze_analytics(
                breeze_token=actual_breeze_token, start_time_iso=start_week_iso_str, end_time_iso=end_time_iso_str,
                shop_id=first_shop.id, shop_url=first_shop.url, shop_type=first_shop.type
            )
            breeze_analytics_weekly_str = json.dumps(breeze_weekly_raw, indent=2) if breeze_weekly_raw else "{}"

        except BreezeAnalyticsError as e:
            logger.error(f"[{session_id}] BreezeAnalyticsError fetching analytics: {e}")
        except ValueError as e:
            logger.error(f"[{session_id}] ValueError for Breeze analytics: {e}")
        except Exception as e:
            logger.error(f"[{session_id}] Unexpected error fetching Breeze analytics: {e}", exc_info=True)


    logger.info(f"[{session_id}] Pre-Gemini API calls completed.")
    return {
        "juspay_analytics_today_str": juspay_analytics_today_str if juspay_analytics_today_str else "{}",
        "breeze_analytics_today_str": breeze_analytics_today_str if breeze_analytics_today_str else "{}",
        "juspay_analytics_weekly_str": juspay_analytics_weekly_str if juspay_analytics_weekly_str else "{}",
        "breeze_analytics_weekly_str": breeze_analytics_weekly_str if breeze_analytics_weekly_str else "{}",
        "current_kolkata_time_str": current_kolkata_time_str if current_kolkata_time_str else "Not available",
    }


async def handle_websocket_session(websocket: WebSocket):
    session_id = f"session_{len(active_connections) + 1}_{int(time.time())}"
    token = websocket.query_params.get("token")
    testmode_param = websocket.query_params.get("testmode", "false").lower()
    is_test_mode = testmode_param == "true"

    use_dummy_data = is_test_mode or not token

    await websocket.accept()
    logger.info(f"[{session_id}] WebSocket connection established. Token: {token}, Test Mode: {is_test_mode}, Use Dummy Data: {use_dummy_data}")
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
        pre_gemini_data = None
        if use_dummy_data:
            ist_timezone = pytz.timezone("Asia/Kolkata")
            now_ist = dt.now(ist_timezone)
            pre_gemini_data = {
                "juspay_analytics_today_str": dummy_juspay_analytics_today,
                "breeze_analytics_today_str": dummy_breeze_analytics_today,
                "juspay_analytics_weekly_str": dummy_juspay_analytics_weekly,
                "breeze_analytics_weekly_str": dummy_breeze_analytics_weekly,
                "current_kolkata_time_str": now_ist.strftime('%Y-%m-%d %H:%M:%S %Z%z')
            }
        else:
            # Perform pre-Gemini calls only if not in test mode and token is present
            pre_gemini_data = await _perform_pre_gemini_calls(token=websocket.state.juspay_token, session_id=session_id)

        # Check for disconnection after long-running analytics call
        if websocket.client_state != WebSocketState.CONNECTED:
            logger.warning(f"[{session_id}] Client disconnected during analytics fetch. Aborting session.")
            return

        logger.info(f"[{session_id}] Proceeding to create Gemini session.")
        gemini_session, gemini_session_cm = await create_gemini_session(
            use_dummy_data=use_dummy_data,
            current_kolkata_time_str=pre_gemini_data.get("current_kolkata_time_str") if pre_gemini_data else None,
            juspay_analytics_today_str=pre_gemini_data.get("juspay_analytics_today_str") if pre_gemini_data else None,
            breeze_analytics_today_str=pre_gemini_data.get("breeze_analytics_today_str") if pre_gemini_data else None,
            juspay_analytics_weekly_str=pre_gemini_data.get("juspay_analytics_weekly_str") if pre_gemini_data else None,
            breeze_analytics_weekly_str=pre_gemini_data.get("breeze_analytics_weekly_str") if pre_gemini_data else None
        )

        # Check for disconnection after Gemini session creation
        if websocket.client_state != WebSocketState.CONNECTED:
            logger.warning(f"[{session_id}] Client disconnected during Gemini session creation. Aborting and cleaning up session.")
            await close_gemini_session(gemini_session_cm)
            return

        logger.info(f"[{session_id}] Gemini session created successfully. Sending initialization_done event.")
        await websocket.send_text(json.dumps({"type": "initialization_done"}))

    except (WebSocketDisconnect, RuntimeError) as e:
        # This will catch cases where the client disconnects while we are trying to send/receive
        if isinstance(e, RuntimeError) and "close message has been sent" in str(e).lower():
            logger.warning(f"[{session_id}] Attempted to operate on a closed websocket during initialization.")
        else:
            logger.info(f"[{session_id}] Client disconnected during initialization. Aborting.")
        # The 'finally' block will handle cleanup, so we just need to exit the function.
        return
    except Exception as e:
        logger.error(f"[{session_id}] A critical error occurred during session initialization: {e}", exc_info=True)
        # Try to inform the client, but expect it might fail if the connection is the issue
        try:
            if websocket.client_state == WebSocketState.CONNECTED:
                await websocket.send_text(json.dumps({"type": "error", "message": "Failed to initialize session"}))
        except (WebSocketDisconnect, RuntimeError):
            logger.warning(f"[{session_id}] Client was already disconnected. Could not send initialization error.")
        # The 'finally' block will handle cleanup
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