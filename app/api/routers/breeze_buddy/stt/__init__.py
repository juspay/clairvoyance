"""Standalone (template-independent) STT endpoints.

Routes under ``/agent/voice/breeze-buddy/stt``:

- ``POST /transcribe``   one-shot audio-clip -> transcript
- ``WS /stream``         realtime audio stream -> partial/final transcripts

Unlike the widget push-to-talk route (``/widget/session/{id}/transcribe``),
these endpoints are bound to no widget session and no template: the caller
passes the STT ``provider`` and (optionally) ``model``/``language`` directly.
Both sit behind the standard RBAC bearer auth, so interactive dashboard users
and machine (S2S) callers can reach them (the WebSocket accepts the token via
``Authorization`` header or ``?token=`` query param).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, UploadFile, WebSocket

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.stt import TranscriptionRequest, TranscriptionResponse

from .handlers import handle_transcription_request, handle_transcription_stream

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


@router.websocket("/stream")
async def transcribe_stream(ws: WebSocket) -> None:
    """Realtime speech-to-text: stream PCM16 audio in, get transcripts out.

    See :func:`handle_transcription_stream` for the message protocol
    (auth -> JSON config -> binary audio -> partial/final events -> stop).
    """
    await handle_transcription_stream(ws)


__all__ = ["router"]
