"""
Inbound call endpoint for Exotel.

This module provides the Voicebot URL endpoint for handling inbound calls.
Uses Voicebot-only architecture - no Passthru applet needed.

Flow: Voicebot applet → WebSocket

Endpoints:
- GET/POST /exotel/voicebot-url - Returns JSON with WebSocket URL

Authentication:
- Requires `auth_token` query parameter matching EXOTEL_WEBHOOK_AUTH_TOKEN env var
- Configure in Exotel dashboard: https://yourserver.com/...?auth_token=YOUR_SECRET
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.core.config.static import EXOTEL_WEBHOOK_AUTH_TOKEN

from .handlers import handle_voicebot_url

router = APIRouter()


def verify_exotel_auth(auth_token: str = Query(None)):
    """
    Verify the auth token for Exotel webhook endpoints.

    Raises HTTPException 401 if:
    - EXOTEL_WEBHOOK_AUTH_TOKEN is not configured (required in all environments)
    - Token doesn't match or is missing
    """
    if not EXOTEL_WEBHOOK_AUTH_TOKEN:
        raise HTTPException(
            status_code=401, detail="Webhook authentication not configured"
        )
    if auth_token != EXOTEL_WEBHOOK_AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid auth token")
    return True


@router.api_route("/exotel/voicebot-url", methods=["GET", "POST"])
async def exotel_voicebot_url(request: Request, _: bool = Depends(verify_exotel_auth)):
    """
    Get WebSocket URL for Exotel Voicebot applet.

    This is the single entry point for all calls (both inbound and outbound).
    No Passthru applet needed - all lookups and IVR audio generation happen here.

    Query Parameters:
        auth_token: Required authentication token (must match EXOTEL_WEBHOOK_AUTH_TOKEN)
        CallSid: Unique identifier for the call
        CallFrom/From: The caller's phone number
        CallTo/To: The number that was called

    Returns:
        JSON response: {"url": "wss://..."}
    """
    return await handle_voicebot_url(request)
