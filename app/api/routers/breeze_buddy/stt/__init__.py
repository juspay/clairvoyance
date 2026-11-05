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

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, UploadFile, WebSocket
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.stt import TranscriptionRequest, TranscriptionResponse

from .handlers import handle_transcription_request, handle_transcription_stream

router = APIRouter(prefix="/stt", tags=["stt"])


# Cap on how much of an invalid value a 422 echoes back in ``input``: the
# nested-config form fields accept JSON strings up to 64 KiB, and a
# validation failure should not reflect that much caller data into the
# error body.
_MAX_ERROR_INPUT_CHARS = 200


def _body_errors(exc: ValidationError) -> List[Dict[str, Any]]:
    """Re-shape manual-validation errors to FastAPI's native 422 format.

    Prefix ``loc`` with ``body`` (as FastAPI's own request validation does),
    drop ``ctx`` (its values may not be JSON-serializable), and truncate
    long string ``input`` echoes.
    """
    errors: List[Dict[str, Any]] = []
    for err in exc.errors(include_url=False):
        cleaned: Dict[str, Any] = {k: v for k, v in err.items() if k != "ctx"}
        cleaned["loc"] = ("body", *err["loc"])
        value = cleaned.get("input")
        if isinstance(value, str) and len(value) > _MAX_ERROR_INPUT_CHARS:
            cleaned["input"] = value[:_MAX_ERROR_INPUT_CHARS] + "…[truncated]"
        errors.append(cleaned)
    return errors


@router.post(
    "/transcribe",
    response_model=TranscriptionResponse,
    summary="Transcribe an audio clip to text (provider/model chosen per-request)",
)
async def transcribe(
    audio: UploadFile = File(
        ..., description="Audio clip: WAV, MP3, WebM/Opus, M4A, OGG, FLAC."
    ),
    provider: str = Form(
        ...,
        description=(
            "STT provider: soniox | deepgram | sarvam | openai | google "
            "(case-insensitive, trimmed)."
        ),
    ),
    model: Optional[str] = Form(
        None,
        description="Model id override; blank/omitted = the provider's default.",
    ),
    language: Optional[str] = Form(
        None,
        description="BCP-47 / ISO-639 language hint; blank/omitted = auto-detect.",
    ),
    soniox: Optional[str] = Form(
        None,
        description=(
            "JSON-encoded SonioxSTTConfig, e.g. "
            '{"context": "Breeze Buddy, UPI"}. A model set here wins over '
            "the flat model field."
        ),
    ),
    deepgram: Optional[str] = Form(
        None,
        description="JSON-encoded DeepgramSTTConfig (smart_format, numerals, ...).",
    ),
    sarvam: Optional[str] = Form(
        None,
        description="JSON-encoded SarvamSTTConfig (language_code, ...).",
    ),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> TranscriptionResponse:
    """One-shot speech-to-text, independent of any widget session or template.

    Upload a short clip plus the ``provider`` (``soniox`` | ``deepgram`` |
    ``sarvam`` | ``openai`` | ``google``) and optional ``model``/``language``;
    get the transcript back. The response reports the provider and model that
    actually produced the text (OpenAI Whisper when the core falls back).

    The form fields are declared individually rather than as
    ``Annotated[TranscriptionRequest, Form()]``: with a ``File`` param in the
    same signature, FastAPI 0.115 embeds the form model under its param name
    (expecting a ``request`` object field) instead of flattening it — a shape
    multipart form data cannot express, so every upload would 422.
    ``TranscriptionRequest`` still does all the validation below.
    """
    try:
        request = TranscriptionRequest.model_validate(
            {
                "provider": provider,
                "model": model,
                "language": language,
                "soniox": soniox,
                "deepgram": deepgram,
                "sarvam": sarvam,
            }
        )
    except ValidationError as exc:
        raise RequestValidationError(_body_errors(exc)) from None
    return await handle_transcription_request(audio, request, current_user)


@router.websocket("/stream")
async def transcribe_stream(ws: WebSocket) -> None:
    """Realtime speech-to-text: stream PCM16 audio in, get transcripts out.

    See :func:`handle_transcription_stream` for the message protocol
    (auth -> JSON config -> binary audio -> partial/final events -> stop).
    """
    await handle_transcription_stream(ws)


__all__ = ["router"]
