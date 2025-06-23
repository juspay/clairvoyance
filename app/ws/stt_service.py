import asyncio
import json
import logging
import time
import functools

from google.cloud import speech_v1 as speech
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

def sync_audio_chunk_generator(audio_queue: asyncio.Queue, current_session_id: str, shutdown_event: asyncio.Event):
    logger.debug(f"[{current_session_id}] SYNC STT audio_chunk_generator started.")
    while not shutdown_event.is_set():
        try:
            chunk = audio_queue.get_nowait()
            if chunk is None:
                logger.debug(f"[{current_session_id}] SYNC STT audio_chunk_generator received None, stopping.")
                break
            yield speech.StreamingRecognizeRequest(audio_content=chunk)
            audio_queue.task_done()
        except asyncio.QueueEmpty:
            continue
        except Exception as e:
            logger.error(f"[{current_session_id}] SYNC STT audio_chunk_generator error: {e}")
            break
    logger.debug(f"[{current_session_id}] SYNC STT audio_chunk_generator finished.")

def run_stt_stream_blocking(
    audio_queue: asyncio.Queue,
    current_session_id: str,
    ws_conn_state: WebSocketState, # Pass only the state for safety
    ws_send_text_func, # Pass the send_text method
    stt_client_sync,
    loop: asyncio.AbstractEventLoop,
    local_stt_finalized_event: asyncio.Event,
    shutdown_event: asyncio.Event,
    sample_rate: int
):
    logger.info(f"[{current_session_id}] Starting BLOCKING STT streaming task in executor.")

    streaming_config = speech.StreamingRecognitionConfig(
        config=speech.RecognitionConfig(
            encoding=speech.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=sample_rate,
            language_code="en_US",
            alternative_language_codes=["en_IN"],
            enable_automatic_punctuation=True,
            model="default",
        ),
        interim_results=True,
    )

    requests_iterable = sync_audio_chunk_generator(audio_queue, current_session_id, shutdown_event)

    try:
        responses = stt_client_sync.streaming_recognize(config=streaming_config, requests=requests_iterable)

        for response in responses:
            if shutdown_event.is_set():
                logger.info(f"[{current_session_id}] STT stream: Shutdown event set, breaking loop.")
                break
            if not response.results:
                continue
            result = response.results[0]
            if not result.alternatives:
                continue
            transcript = result.alternatives[0].transcript

            if result.is_final:
                if ws_conn_state == WebSocketState.CONNECTED:
                    message_to_send = {
                        "type": "input_transcript",
                        "text": transcript,
                        "is_final": True,
                        "source": "local_google_stt_streaming"
                    }
                    # Use the passed send_text function
                    asyncio.run_coroutine_threadsafe(ws_send_text_func(json.dumps(message_to_send)), loop)
                    logger.info(f"[{current_session_id}] Sent FINAL STT transcript: {transcript}")
                    if local_stt_finalized_event:
                       loop.call_soon_threadsafe(local_stt_finalized_event.set)
                else:
                    logger.warning(f"[{current_session_id}] STT stream: WebSocket no longer connected, cannot send final transcript.")
                    break
            else:
                logger.debug(f"[{current_session_id}] Received INTERIM STT transcript (not sent to client): {transcript}")

            if ws_conn_state != WebSocketState.CONNECTED:
                logger.warning(f"[{current_session_id}] STT stream: WebSocket no longer connected, breaking loop.")
                break
    except Exception as e:
        logger.error(f"[{current_session_id}] Error in BLOCKING STT streaming: {e}", exc_info=True)
    finally:
        logger.info(f"[{current_session_id}] BLOCKING STT streaming task finished in executor.")
        if local_stt_finalized_event and not local_stt_finalized_event.is_set(): # Check if not already set
            logger.warning(f"[{current_session_id}] STT task ending, ensuring local_stt_finalized_event is set.")
            loop.call_soon_threadsafe(local_stt_finalized_event.set) # Ensure it's set

        if audio_queue:
            try:
                # Ensure the queue is emptied and None is put to signal generator if it's still running
                while not audio_queue.empty():
                    audio_queue.get_nowait()
                    audio_queue.task_done()
                audio_queue.put_nowait(None)
            except asyncio.QueueFull:
                logger.warning(f"[{current_session_id}] STT audio queue full when trying to signal stop in finally.")
            except Exception as e_q:
                logger.error(f"[{current_session_id}] Error putting None to STT audio queue in finally: {e_q}")
