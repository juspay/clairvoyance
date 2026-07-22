"""Handlers for the standalone (template-independent) STT endpoints."""

import asyncio
import json
import time

from fastapi import HTTPException, UploadFile, WebSocket, WebSocketDisconnect, status
from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.stt import create_stt_from_config
from app.ai.voice.agents.breeze_buddy.template.types import (
    DeepgramSTTConfig,
    SarvamSTTConfig,
    SonioxSTTConfig,
    STTConfiguration,
    STTProvider,
)
from app.ai.voice.agents.breeze_buddy.utils.transport.websockets import (
    close_websocket_safely,
    send_message,
)
from app.ai.voice.stt import TranscriptionError, transcribe_audio
from app.ai.voice.stt.streaming import StreamingTranscriber, TranscriptEvent
from app.api.security.breeze_buddy.rbac_token import get_user_from_websocket
from app.core.config.dynamic import (
    STT_MAX_AUDIO_BYTES,
    STT_STREAM_IDLE_TIMEOUT_SECONDS,
    STT_STREAM_MAX_SECONDS,
)
from app.core.config.static import SAMPLE_RATE
from app.core.logger import logger
from app.schemas import UserInfo
from app.schemas.breeze_buddy.stt import (
    TranscriptionRequest,
    TranscriptionResponse,
    TranscriptionStreamRequest,
)

# Seconds the client gets to send the JSON config message after connecting.
_STREAM_CONFIG_TIMEOUT_SECONDS = 10.0

# Application close codes (4xxx range is free for applications).
_WS_BAD_REQUEST = 4400
_WS_UNAUTHORIZED = 4401
_WS_TIMEOUT = 4408


async def handle_transcription_request(
    audio: UploadFile,
    request: TranscriptionRequest,
    current_user: UserInfo,
) -> TranscriptionResponse:
    """Transcribe an uploaded clip with a caller-chosen provider/model.

    Stateless and template-independent: no widget session, no template lookup.
    The clip is read with a size guard, then handed to the shared
    ``transcribe_audio`` core (Soniox uses its async file API; Google — and any
    provider whose key is unset — falls back to Whisper inside that core). The
    response reports the provider/model that actually produced the text.
    """
    max_bytes = await STT_MAX_AUDIO_BYTES()
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await audio.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"Audio exceeds the {max_bytes}-byte limit",
            )
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty audio upload",
        )

    try:
        result = await transcribe_audio(
            data,
            audio.content_type,
            provider=request.provider.value,
            model=request.model,
            language=request.language,
            filename=audio.filename or "audio.webm",
        )
    except TranscriptionError as e:
        logger.warning(
            "stt transcribe failed (provider={}, user={}): {}",
            request.provider.value,
            current_user.id,
            e,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription failed",
        ) from e

    return TranscriptionResponse(
        text=result.text, provider=result.provider, model=result.model
    )


def _stream_configuration(request: TranscriptionStreamRequest) -> STTConfiguration:
    """Map the stream config message onto the template ``STTConfiguration``."""
    model = request.model
    provider = request.provider
    return STTConfiguration(
        provider=provider,
        language=request.language,
        soniox=(
            SonioxSTTConfig(model=model)
            if provider == STTProvider.SONIOX and model
            else None
        ),
        deepgram=(
            DeepgramSTTConfig(model=model)
            if provider == STTProvider.DEEPGRAM and model
            else None
        ),
        sarvam=(
            SarvamSTTConfig(model=model)
            if provider == STTProvider.SARVAM and model
            else None
        ),
    )


async def _reject_stream(ws: WebSocket, code: int, message: str, reason: str) -> None:
    """Send a final error event and close the stream."""
    await send_message(ws, {"type": "error", "message": message})
    await close_websocket_safely(ws, code=code, reason=reason)


async def handle_transcription_stream(ws: WebSocket) -> None:
    """Realtime speech-to-text over WebSocket.

    Protocol:

    1. Client connects with the RBAC bearer token (``Authorization`` header or
       ``?token=`` query param).
    2. Client sends one JSON text message — :class:`TranscriptionStreamRequest`
       (``provider``, optional ``model``/``language``/``sample_rate``).
    3. Server replies ``{"type": "ready"}``, then the client streams binary
       frames of raw PCM16 mono audio at ``sample_rate``.
    4. Server pushes ``{"type": "partial" | "final", "text", "language"}``
       events as the provider produces them.
    5. Client sends ``{"type": "stop"}`` (or disconnects) to end; the server
       flushes remaining finals and closes.

    Streams are bounded by ``STT_STREAM_MAX_SECONDS`` and
    ``STT_STREAM_IDLE_TIMEOUT_SECONDS`` (both dynamic config).
    """
    await ws.accept()

    try:
        user = get_user_from_websocket(ws)
    except HTTPException as e:
        await _reject_stream(ws, _WS_UNAUTHORIZED, str(e.detail), "unauthorized")
        return

    try:
        raw_config = await asyncio.wait_for(
            ws.receive_text(), timeout=_STREAM_CONFIG_TIMEOUT_SECONDS
        )
    except (WebSocketDisconnect, asyncio.TimeoutError):
        await close_websocket_safely(ws, code=_WS_BAD_REQUEST, reason="no config")
        return

    try:
        request = TranscriptionStreamRequest.model_validate_json(raw_config)
    except ValidationError as e:
        await _reject_stream(
            ws, _WS_BAD_REQUEST, f"Invalid stream config: {e}", "invalid config"
        )
        return

    if request.provider == STTProvider.OPENAI:
        await _reject_stream(
            ws,
            _WS_BAD_REQUEST,
            "openai has no realtime streaming path; use POST /stt/transcribe",
            "unsupported provider",
        )
        return
    if request.provider == STTProvider.GOOGLE and request.model:
        await _reject_stream(
            ws,
            _WS_BAD_REQUEST,
            "model override is not supported for google streams",
            "invalid config",
        )
        return
    if request.provider == STTProvider.SARVAM and request.sample_rate != SAMPLE_RATE:
        await _reject_stream(
            ws,
            _WS_BAD_REQUEST,
            f"sarvam streams require sample_rate={SAMPLE_RATE}",
            "invalid config",
        )
        return

    try:
        stt_service = await create_stt_from_config(_stream_configuration(request))
    except ValueError as e:
        await _reject_stream(ws, _WS_BAD_REQUEST, str(e), "provider unavailable")
        return

    async def _forward(event: TranscriptEvent) -> None:
        await send_message(
            ws,
            {
                "type": "final" if event.is_final else "partial",
                "text": event.text,
                "language": event.language,
            },
        )

    transcriber = StreamingTranscriber(
        stt_service, sample_rate=request.sample_rate, on_transcript=_forward
    )
    max_seconds = await STT_STREAM_MAX_SECONDS()
    idle_timeout = await STT_STREAM_IDLE_TIMEOUT_SECONDS()

    await transcriber.start()
    await send_message(ws, {"type": "ready"})
    logger.info(
        "stt stream started (provider={}, user={}, sample_rate={})",
        request.provider.value,
        user.id,
        request.sample_rate,
    )

    close_code, close_reason = 1000, "done"
    deadline = time.monotonic() + max_seconds
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await send_message(
                    ws,
                    {
                        "type": "error",
                        "message": f"stream exceeded the {max_seconds}s limit",
                    },
                )
                close_code, close_reason = _WS_TIMEOUT, "max duration exceeded"
                break

            try:
                message = await asyncio.wait_for(
                    ws.receive(), timeout=min(float(idle_timeout), remaining)
                )
            except asyncio.TimeoutError:
                if deadline - time.monotonic() <= 0:
                    detail = f"stream exceeded the {max_seconds}s limit"
                    close_reason = "max duration exceeded"
                else:
                    detail = f"no client data for {idle_timeout}s"
                    close_reason = "idle timeout"
                await send_message(ws, {"type": "error", "message": detail})
                close_code = _WS_TIMEOUT
                break

            if message["type"] == "websocket.disconnect":
                close_code, close_reason = 1000, "client disconnected"
                break

            audio = message.get("bytes")
            if audio:
                await transcriber.feed(audio)
                continue

            text = message.get("text")
            if not text:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(control, dict) and control.get("type") == "stop":
                break
    except Exception as e:
        logger.error(
            "stt stream failed (provider={}, user={}): {}",
            request.provider.value,
            user.id,
            e,
        )
        await send_message(ws, {"type": "error", "message": "Internal stream error"})
        close_code, close_reason = 1011, "internal error"
    finally:
        # stop() flushes the provider's trailing finals through _forward
        # before the socket closes.
        await transcriber.stop()
        await close_websocket_safely(ws, code=close_code, reason=close_reason)
