"""Handlers for the standalone (template-independent) STT endpoints."""

from fastapi import HTTPException, UploadFile, status

from app.ai.voice.stt import TranscriptionError, transcribe_audio
from app.core.config.dynamic import STT_MAX_AUDIO_BYTES
from app.core.logger import logger
from app.schemas import UserInfo
from app.schemas.breeze_buddy.stt import TranscriptionRequest, TranscriptionResponse


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
