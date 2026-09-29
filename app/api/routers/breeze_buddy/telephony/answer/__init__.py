"""
Unified answer endpoint for all telephony providers.

This module provides the /{provider}/answer endpoint that handles both
inbound and outbound calls for any supported provider (Exotel, Plivo, Vobiz).

Flow: Provider webhook -> resolve templates -> return provider-specific response

Endpoints:
- GET/POST /{provider}/answer - Unified answer handler

Authentication:
- Exotel: Requires `auth_token` query parameter matching EXOTEL_WEBHOOK_AUTH_TOKEN env var
- Plivo / Vobiz: No authentication (parity; signature checks are a follow-up)
"""

from fastapi import APIRouter, HTTPException, Request

from app.core.config.static import EXOTEL_WEBHOOK_AUTH_TOKEN

from .handlers import handle_provider_answer

router = APIRouter()

SUPPORTED_ANSWER_PROVIDERS = {"exotel", "plivo", "vobiz"}


@router.api_route("/{provider}/answer", methods=["GET", "POST"])
async def provider_answer(request: Request, provider: str):
    """
    Unified answer endpoint for telephony providers.

    When a call is answered, the telephony provider hits this endpoint.
    Resolves templates and returns a provider-appropriate response:
    - Exotel: JSON ``{"url": "wss://..."}``
    - Plivo: XML ``<Stream>`` or ``<GetInput>``
    - Vobiz: XML ``<Stream>`` (outbound only for now)

    Path Parameters:
        provider: Telephony provider name ("exotel", "plivo" or "vobiz")

    Query Parameters (Exotel):
        auth_token: Required authentication token
        CallSid: Unique call identifier
        CallFrom/From: Caller's phone number
        CallTo/To: Called number

    Form Data (Plivo / Vobiz):
        CallUUID: Unique call identifier
        From: Caller's phone number
        To: Called number
    """
    provider_lower = provider.lower()

    if provider_lower not in SUPPORTED_ANSWER_PROVIDERS:
        raise HTTPException(
            status_code=404,
            detail=f"Provider '{provider}' is not supported for answer webhooks",
        )

    # Exotel requires auth token verification
    if provider_lower == "exotel":
        auth_token = request.query_params.get("auth_token")
        if not EXOTEL_WEBHOOK_AUTH_TOKEN:
            raise HTTPException(
                status_code=401, detail="Webhook authentication not configured"
            )
        if auth_token != EXOTEL_WEBHOOK_AUTH_TOKEN:
            raise HTTPException(status_code=401, detail="Invalid auth token")

    return await handle_provider_answer(request, provider_lower)
