"""Standalone (template-independent) STT endpoints.

Routes under ``/agent/voice/breeze-buddy/stt``:

- ``POST /transcribe``   one-shot audio-clip -> transcript

Unlike the widget push-to-talk route (``/widget/session/{id}/transcribe``),
this endpoint is bound to no widget session and no template: the caller passes
the STT ``provider`` and (optionally) ``model``/``language`` directly. It sits
behind the standard RBAC bearer auth, so both interactive dashboard users and
machine (S2S) callers can reach it.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.stt import TranscriptionRequest, TranscriptionResponse

from .handlers import handle_transcription_request

router = APIRouter(prefix="/stt", tags=["stt"])


@router.post(
    "/transcribe",
    response_model=TranscriptionResponse,
    summary="Transcribe an audio clip to text (provider/model chosen per-request)",
)
async def transcribe(
    request: Annotated[TranscriptionRequest, Form()],
    audio: UploadFile = File(...),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TranscriptionResponse:
    """One-shot speech-to-text, independent of any widget session or template.

    Upload a short clip plus the ``provider`` (``soniox`` | ``deepgram`` |
    ``sarvam`` | ``openai`` | ``google``) and optional ``model``/``language``;
    get the transcript back. The response reports the provider and model that
    actually produced the text (OpenAI Whisper when the core falls back).
    """
    return await handle_transcription_request(audio, request, current_user)


__all__ = ["router"]
