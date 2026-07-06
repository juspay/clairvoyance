"""Handlers for the global (template-independent) STT endpoints."""

from typing import Optional

from fastapi import HTTPException, UploadFile, status

from app.ai.voice.agents.breeze_buddy.template.types import STTProvider
from app.ai.voice.stt import TranscriptionError, transcribe_audio
from app.core.config.dynamic import WIDGET_STT_MAX_AUDIO_BYTES
from app.core.logger import logger
from app.schemas.breeze_buddy.stt import GlobalTranscribeResponse

_VALID_PROVIDERS = {p.value for p in STTProvider}


async def transcribe_audio_handler(
    audio: UploadFile,
    provider: str,
    model: Optional[str],
    language: Optional[str],
) -> GlobalTranscribeResponse:
    """Transcribe an uploaded clip with a caller-chosen provider/model.

    Stateless and template-independent: no widget session, no template lookup.
    The clip is read with the shared size guard, then handed to the shared
    ``transcribe_audio`` core (Soniox uses its async file API; Google — and any
    provider whose key is unset — falls back to Whisper inside that core).
    """
    prov = (provider or "").strip().lower()
    if prov not in _VALID_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unknown STT provider '{provider}'. "
                f"Valid providers: {sorted(_VALID_PROVIDERS)}"
            ),
        )

    max_bytes = await WIDGET_STT_MAX_AUDIO_BYTES()
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

    model_override = model.strip() if model and model.strip() else None
    lang = language.strip() if language and language.strip() else None

    try:
        result = await transcribe_audio(
            data,
            audio.content_type,
            provider=prov,
            model=model_override,
            language=lang,
            filename=audio.filename or "audio.webm",
        )
    except TranscriptionError as e:
        logger.warning("global transcribe failed (provider={}): {}", prov, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Transcription failed",
        ) from e

    response_model = model_override if result.provider == prov else None
    return GlobalTranscribeResponse(
        text=result.text, provider=result.provider, model=response_model
    )
