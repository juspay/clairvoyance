"""
RESTful TTS voice catalog endpoints.

Endpoints:
- GET  /tts/voices           - List enabled voices, optionally filtered by provider/language
- POST /tts/voices/reconcile - Regenerate stale/missing previews for enabled voices (admin only)
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Header, Query, Response

from app.api.routers.breeze_buddy.numbers.rbac import require_admin_access
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.schemas import UserInfo
from app.schemas.breeze_buddy.tts_catalog import (
    ReconcileReportResponse,
    VoicesResponse,
)

from . import handlers

router = APIRouter()

# NOTE: FastAPI's Query(enum=...) only annotates the OpenAPI schema on this
# FastAPI version — it does not reject invalid values at runtime, so a Literal
# is needed to enforce the 422 the test pins. Values must be spelled out
# statically (mirroring CATALOG_PROVIDERS) rather than built from the tuple —
# pyrefly rejects `Literal[some_tuple_variable]` as not-a-type.
ProviderFilter = Literal[
    "elevenlabs", "cartesia", "sarvam", "gemini", "google", "soniox"
]


@router.get("/tts/voices", response_model=VoicesResponse)
async def list_voices(
    response: Response,
    provider: Optional[ProviderFilter] = Query(None),
    language: Optional[str] = Query(None, min_length=2, max_length=16),
    if_none_match: Optional[str] = Header(None),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    List enabled TTS voices.

    Query Parameters:
    - provider: Optional filter by provider (elevenlabs, cartesia, sarvam,
      gemini, google, soniox)
    - language: Optional filter by language code (bare or regional, e.g.
      "en" matches "en-IN")

    Response is cached client-side for 5 minutes; a matching If-None-Match
    header returns 304 with no body.
    """
    body, etag = await handlers.list_voices_handler(provider, language)
    response.headers["Cache-Control"] = "private, max-age=300"
    response.headers["ETag"] = etag
    if if_none_match == etag:
        return Response(
            status_code=304,
            headers={"ETag": etag, "Cache-Control": "private, max-age=300"},
        )
    return body


@router.post("/tts/voices/reconcile", response_model=ReconcileReportResponse)
async def reconcile_voices(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """
    Regenerate stale or missing previews for every enabled voice.

    For each enabled voice and language, skips generation when a stored
    preview already matches the current content key; otherwise generates and
    stores a fresh preview. Failures are recorded per-language rather than
    aborting the run.

    Permissions:
    - Admin only
    """
    require_admin_access(current_user, "reconcile tts voice previews")
    return await handlers.reconcile_previews_handler()
