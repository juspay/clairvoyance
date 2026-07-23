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

# Per-frame cap on binary audio messages. Generous for real clients (~2.5s of
# 48kHz PCM16 mono) while bounding what one frame can buffer server-side.
_MAX_CHUNK_BYTES = 256 * 1024

# Application close codes (4xxx range is free for applications).
_WS_BAD_REQUEST = 4400
_WS_UNAUTHORIZED = 4401
_WS_TIMEOUT = 4408


def _effective_model(
    cfg: SonioxSTTConfig | DeepgramSTTConfig | SarvamSTTConfig,
    flat_model: str | None,
) -> str | None:
    """Uniform model precedence across providers.

    A model *explicitly set* in the nested config wins; otherwise the flat
    ``model`` shortcut fills in; otherwise ``None`` so the provider call's
    own default applies. ``model_fields_set`` distinguishes "caller set the
    nested model" from a schema default like Deepgram's ``nova-3-general`` —
    a plain truthiness check would silently discard the flat model there,
    and forwarding schema defaults would wrongly flip on strict mode (and
    push template *streaming* model defaults into the batch path).
    """
    if "model" in cfg.model_fields_set and cfg.model:
        return cfg.model
    return flat_model


def _batch_model_and_options(
    request: TranscriptionRequest,
) -> tuple[str | None, dict | None]:
    """Resolve the effective model + provider extras for the batch core.

    Model precedence is :func:`_effective_model`; only options meaningful to
    the one-shot provider APIs are forwarded (streaming-only knobs like
    Deepgram endpointing stay behind).
    """
    provider = request.provider
    if provider == STTProvider.SONIOX and request.soniox:
        cfg = request.soniox
        opts = {
            key: value
            for key, value in {
                "context": cfg.context,
                "enable_language_identification": cfg.enable_language_identification,
            }.items()
            if value is not None
        }
        return _effective_model(cfg, request.model), opts or None
    if provider == STTProvider.DEEPGRAM and request.deepgram:
        cfg = request.deepgram
        # Only explicitly-set flags are forwarded (schema defaults stay
        # behind): keeps strict-mode semantics uniform with Soniox/Sarvam —
        # an empty nested config pins nothing.
        opts = {
            key: getattr(cfg, key)
            for key in (
                "smart_format",
                "punctuate",
                "numerals",
                "profanity_filter",
                "diarize",
                "auto_detect_language",
            )
            if key in cfg.model_fields_set
        }
        return _effective_model(cfg, request.model), opts or None
    if provider == STTProvider.SARVAM and request.sarvam:
        cfg = request.sarvam
        opts = {"language_code": cfg.language_code} if cfg.language_code else None
        return _effective_model(cfg, request.model), opts
    return request.model, None


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
    Provider-specific tuning arrives via the request's nested configs and is
    resolved by :func:`_batch_model_and_options`. Explicitly pinned
    model/options run in strict mode: provider failure returns 502 instead of
    silently degrading to a Whisper transcript.
    """
    if request.provider == STTProvider.GOOGLE and request.model:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="model override is not supported for google",
        )

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

    model, provider_options = _batch_model_and_options(request)
    try:
        result = await transcribe_audio(
            data,
            audio.content_type,
            provider=request.provider.value,
            model=model,
            language=request.language,
            filename=audio.filename or "audio.webm",
            options=provider_options,
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
    """Map the stream config message onto the template ``STTConfiguration``.

    The selected provider's nested config passes through — full parity with
    template STT settings (Soniox context, Deepgram endpointing, ...). Model
    precedence matches the batch path (:func:`_effective_model`): an
    explicitly set nested model wins, otherwise the flat ``model`` shortcut
    fills in. Nested configs for other providers are dropped.
    """
    provider = request.provider
    model = request.model
    soniox = request.soniox if provider == STTProvider.SONIOX else None
    deepgram = request.deepgram if provider == STTProvider.DEEPGRAM else None
    sarvam = request.sarvam if provider == STTProvider.SARVAM else None
    if model:
        if provider == STTProvider.SONIOX:
            if soniox is None:
                soniox = SonioxSTTConfig(model=model)
            elif "model" not in soniox.model_fields_set:
                soniox = soniox.model_copy(update={"model": model})
        elif provider == STTProvider.DEEPGRAM:
            if deepgram is None:
                deepgram = DeepgramSTTConfig(model=model)
            elif "model" not in deepgram.model_fields_set:
                deepgram = deepgram.model_copy(update={"model": model})
        elif provider == STTProvider.SARVAM:
            if sarvam is None:
                sarvam = SarvamSTTConfig(model=model)
            elif "model" not in sarvam.model_fields_set:
                sarvam = sarvam.model_copy(update={"model": model})
    return STTConfiguration(
        provider=provider,
        language=request.language,
        soniox=soniox,
        deepgram=deepgram,
        sarvam=sarvam,
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
    ``STT_STREAM_IDLE_TIMEOUT_SECONDS`` (both dynamic config); individual
    binary frames are capped at ``_MAX_CHUNK_BYTES``. Provider startup
    failures are rejected with close code 4400 before ``ready`` is sent;
    provider death or client send failures mid-stream close with 1011.
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

    send_failed = asyncio.Event()

    async def _forward(event: TranscriptEvent) -> None:
        ok = await send_message(
            ws,
            {
                "type": "final" if event.is_final else "partial",
                "text": event.text,
                "language": event.language,
            },
        )
        if not ok:
            # Flag for the receive loop; transcripts are being dropped, so
            # the stream must tear down rather than keep paying the provider.
            send_failed.set()

    transcriber = StreamingTranscriber(
        stt_service, sample_rate=request.sample_rate, on_transcript=_forward
    )
    max_seconds = await STT_STREAM_MAX_SECONDS()
    idle_timeout = await STT_STREAM_IDLE_TIMEOUT_SECONDS()

    try:
        await transcriber.start()
    except RuntimeError as e:
        logger.warning(
            "stt stream provider startup failed (provider={}, user={}): {}",
            request.provider.value,
            user.id,
            e,
        )
        await _reject_stream(
            ws, _WS_BAD_REQUEST, "provider connection failed", "provider unavailable"
        )
        return
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
            if transcriber.failed:
                await send_message(
                    ws, {"type": "error", "message": "provider connection lost"}
                )
                close_code, close_reason = 1011, "provider error"
                break
            if send_failed.is_set():
                close_code, close_reason = 1011, "client send failed"
                break

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
                if len(audio) > _MAX_CHUNK_BYTES:
                    await send_message(
                        ws,
                        {
                            "type": "error",
                            "message": (
                                f"audio chunk exceeds {_MAX_CHUNK_BYTES} bytes"
                            ),
                        },
                    )
                    close_code, close_reason = _WS_BAD_REQUEST, "chunk too large"
                    break
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
        # Broad by design: this loop must reach the close path below no
        # matter what fails; logger.exception preserves the traceback for
        # root-cause analysis.
        logger.exception(
            "stt stream failed (provider={}, user={}): {}: {}",
            request.provider.value,
            user.id,
            type(e).__name__,
            e,
        )
        await send_message(ws, {"type": "error", "message": "Internal stream error"})
        close_code, close_reason = 1011, "internal error"
    finally:
        # stop() flushes the provider's trailing finals through _forward
        # before the socket closes.
        await transcriber.stop()
        await close_websocket_safely(ws, code=close_code, reason=close_reason)
