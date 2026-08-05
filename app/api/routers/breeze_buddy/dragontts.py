"""DragonTTS management endpoints (admin-only).

Manual control of the one-way DragonTTS health flag:

- ``POST /admin/dragontts/manage`` — ``kill_switch`` marks DragonTTS unhealthy
  (calls bypass caching) or ``restore`` marks it healthy (calls resume
  caching). A single atomic Redis SET.
- ``GET  /admin/dragontts/status`` — current health state.

The automatic monitor runs independently and only ever marks the flag
unhealthy (1->0); the manage endpoint is the only way to restore it.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from app.ai.voice.agents.breeze_buddy.tts.dragontts.kill_switch import (
    get_dragontts_status,
    set_dragontts_health,
)
from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.config.dynamic import DRAGONTTS_URL
from app.core.security.authorization import require_admin
from app.schemas import UserInfo
from app.schemas.breeze_buddy.dragontts import (
    DragonTTSStatus,
    ManageDragonTTSAction,
    ManageDragonTTSRequest,
    ManageDragonTTSResponse,
)

router = APIRouter()


@router.post("/admin/dragontts/manage", response_model=ManageDragonTTSResponse)
async def manage_dragon_tts(
    body: ManageDragonTTSRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> ManageDragonTTSResponse:
    """Manage the DragonTTS health flag: ``kill_switch`` -> bypass caching,
    ``restore`` -> resume caching."""
    require_admin(current_user)
    await set_dragontts_health(body.action == ManageDragonTTSAction.RESTORE)
    status = await get_dragontts_status()
    return ManageDragonTTSResponse(action=body.action, **status)


@router.get(
    "/admin/dragontts/status",
    response_model=DragonTTSStatus,
)
async def get_dragon_tts_status(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
) -> DragonTTSStatus:
    """Return the current DragonTTS health-gate state (health + caching)."""
    require_admin(current_user)
    return DragonTTSStatus(**await get_dragontts_status())


# --- DragonTTS cache browser/management proxy (admin-only) -------------------
# DragonTTS is NOT internet-exposed; the dashboard reaches its cache API through
# these admin routes, which forward to the internal service and relay the
# response verbatim (JSON or binary audio). Auth matches the manage/status
# routes above: get_current_user_with_rbac (Depends) + in-body require_admin.

_PROXY_TIMEOUT = 30.0


async def _forward(method: str, path: str, **kwargs) -> Response:
    """Forward a request to the internal DragonTTS service and relay the
    response (status, bytes, content-type). HTTPException(502) if DragonTTS is
    unreachable; otherwise DragonTTS's own status is mirrored (e.g. 404)."""
    url = f"{await DRAGONTTS_URL()}{path}"
    try:
        async with httpx.AsyncClient(timeout=_PROXY_TIMEOUT) as client:
            resp = await client.request(method, url, **kwargs)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"DragonTTS unreachable: {e}")
    # Relay DragonTTS's response verbatim, INCLUDING non-2xx. DragonTTS already
    # returns a JSON {"detail": "upstream <provider> returned an error: ..."}
    # with the real provider reason; re-raising as HTTPException(detail=resp.text)
    # would double-wrap it ({detail: "{\"detail\": ...}"}) and swap the
    # content-type, so the dashboard would lose the structured body. Mirror the
    # upstream status + body + content-type as-is.
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type"),
        status_code=resp.status_code,
    )


@router.get("/admin/dragontts/cache/by-text")
async def proxy_cache_by_text(
    text: str = Query(..., description="text to look up"),
    match: str = Query("exact", description="'exact' or 'substring'"),
    provider: str | None = None,
    voice_id: str | None = None,
    include_audio: bool = Query(False, description="inline base64 audio per entry"),
    limit: int = Query(100, ge=1, le=1000),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Browse every cached variant of a text (metadata + params + hash, optional
    inline audio). Proxied to DragonTTS GET /cache/by-text."""
    require_admin(current_user)
    params = {
        "text": text,
        "match": match,
        "include_audio": include_audio,
        "limit": limit,
    }
    if provider is not None:
        params["provider"] = provider
    if voice_id is not None:
        params["voice_id"] = voice_id
    return await _forward("GET", "/cache/by-text", params=params)


@router.get("/admin/dragontts/cache/{key}")
async def proxy_cache_get(
    key: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    """Download a cached entry's audio by hash. Proxied to DragonTTS GET /cache/{key}."""
    require_admin(current_user)
    return await _forward("GET", f"/cache/{key}")


@router.delete("/admin/dragontts/cache/{key}")
async def proxy_cache_delete(
    key: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    """Delete a cached entry by hash. Proxied to DragonTTS DELETE /cache/{key}."""
    require_admin(current_user)
    return await _forward("DELETE", f"/cache/{key}")


@router.post("/admin/dragontts/cache/{key}/resynth")
async def proxy_cache_resynth(
    key: str, current_user: UserInfo = Depends(get_current_user_with_rbac)
):
    """Re-synthesize an entry from its stored metadata. Proxied to DragonTTS
    POST /cache/{key}/resynth."""
    require_admin(current_user)
    return await _forward("POST", f"/cache/{key}/resynth")


@router.post("/admin/dragontts/cache/{key}/audio")
async def proxy_cache_replace_audio(
    key: str,
    request: Request,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Replace an entry's audio with a user-supplied WAV (base64 JSON body).
    Proxied verbatim to DragonTTS POST /cache/{key}/audio."""
    require_admin(current_user)
    raw = await request.body()
    return await _forward(
        "POST",
        f"/cache/{key}/audio",
        content=raw,
        headers={"content-type": "application/json"},
    )
