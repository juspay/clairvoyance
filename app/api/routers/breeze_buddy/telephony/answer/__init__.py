"""
Unified answer endpoint for all telephony providers.

This module provides the /{provider}/answer endpoint that handles both
inbound and outbound calls. Only Plivo is in use; any other provider is
rejected with 404.

Flow: Provider webhook -> resolve templates -> return provider-specific response

Endpoints:
- GET/POST /{provider}/answer - Unified answer handler

Authentication:
- Plivo: X-Plivo-Signature-V3 HMAC verification against PLIVO_AUTH_TOKEN
  (fail-closed if PLIVO_AUTH_TOKEN is unset)
"""

from fastapi import APIRouter, HTTPException, Request

from app.ai.voice.agents.breeze_buddy.services.telephony.plivo.security import (
    verify_plivo_webhook,
)

from .handlers import handle_provider_answer

router = APIRouter()

SUPPORTED_ANSWER_PROVIDERS = {"plivo"}


@router.api_route("/{provider}/answer", methods=["GET", "POST"])
async def provider_answer(request: Request, provider: str):
    """
    Unified answer endpoint for telephony providers.

    When a call is answered, the telephony provider hits this endpoint.
    Resolves templates and returns Plivo XML (``<Stream>`` or ``<GetInput>``).

    Path Parameters:
        provider: Telephony provider name; only "plivo" is accepted

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

    await verify_plivo_webhook(request)

    return await handle_provider_answer(request, provider_lower)
