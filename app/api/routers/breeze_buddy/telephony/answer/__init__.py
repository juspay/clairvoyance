"""
Unified answer endpoint for all telephony providers.

This module provides the /{provider}/answer endpoint that handles both
inbound and outbound calls for any supported provider (Exotel, Plivo).

Flow: Provider webhook -> resolve templates -> return provider-specific response

Endpoints:
- GET/POST /{provider}/answer - Unified answer handler

Authentication:
- Exotel: constant-time `auth_token` query-param check against EXOTEL_WEBHOOK_AUTH_TOKEN
- Plivo: X-Plivo-Signature-V3/V2 HMAC verification against PLIVO_AUTH_TOKEN
Both go through the shared ``verify_provider_webhook`` (fail-closed if the
provider's secret is unset).
"""

from fastapi import APIRouter, HTTPException, Request

from app.core.security.webhook_signature import verify_provider_webhook

from .handlers import handle_provider_answer

router = APIRouter()

SUPPORTED_ANSWER_PROVIDERS = {"exotel", "plivo"}


@router.api_route("/{provider}/answer", methods=["GET", "POST"])
async def provider_answer(request: Request, provider: str):
    """
    Unified answer endpoint for telephony providers.

    When a call is answered, the telephony provider hits this endpoint.
    Resolves templates and returns a provider-appropriate response:
    - Exotel: JSON ``{"url": "wss://..."}``
    - Plivo: XML ``<Stream>`` or ``<GetInput>``

    Path Parameters:
        provider: Telephony provider name ("exotel" or "plivo")

    Query Parameters (Exotel):
        auth_token: Required authentication token
        CallSid: Unique call identifier
        CallFrom/From: Caller's phone number
        CallTo/To: Called number

    Form Data (Plivo):
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

    # Verify the provider webhook before dispatching (Exotel: constant-time
    # auth_token compare; Plivo: X-Plivo-Signature HMAC). Previously Plivo had
    # NO authentication and Exotel used a non-constant-time compare (PT-23).
    await verify_provider_webhook(request, provider_lower)

    return await handle_provider_answer(request, provider_lower)
