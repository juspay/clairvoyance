"""Meta Graph API calls for WhatsApp onboarding and template management —
NOT a send-path adapter, so this sits outside providers/ (boundary rule 11
confines app.crm.connectivity.providers to send.py and providers/ itself;
these calls are onboarding.py/templates.py's business, not a message send).

Mirrors nautilus's working three steps (exchange code -> short-lived token,
exchange -> long-lived token, verify the frontend-supplied phone_number_id
against the account's real phone number list) — the same provider, a
different app, so the same Graph semantics apply.

Known gaps, carried over from nautilus and not silently closed here:
phone ``/register`` (2FA PIN activation) and ``/debug_token`` introspection
are stubbed, not omitted — real follow-up work, not an oversight.
"""

from typing import Any, Dict, List

import httpx

from app.core.config.static import (
    META_APP_ID,
    META_APP_SECRET,
    META_WHATSAPP_GRAPH_BASE_URL,
    META_WHATSAPP_GRAPH_VERSION,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

# The credential bundle key the send path reads (canon T11 col 6). Named
# once so onboarding's write and templates.py's reads cannot drift apart.
TOKEN_KEY = "system_user_token"


class WhatsappProviderError(Exception):
    """Any Meta Graph API call that fails or returns an unusable shape."""


def _endpoint(path: str) -> str:
    return f"{META_WHATSAPP_GRAPH_BASE_URL}/{META_WHATSAPP_GRAPH_VERSION}/{path}"


async def exchange_code_for_token(code: str) -> str:
    """Embedded Signup code -> short-lived user access token."""
    params = {
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "code": code,
    }
    try:
        async with create_http_client() as client:
            resp = await client.get(_endpoint("oauth/access_token"), params=params)
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta code exchange transport error: {e}")
        raise WhatsappProviderError("failed to exchange code for token") from e
    if resp.status_code != 200 or "access_token" not in body:
        logger.error(f"Meta code exchange failed: {body}")
        raise WhatsappProviderError("failed to exchange code for token")
    return body["access_token"]


async def exchange_for_long_lived_token(short_lived_token: str) -> str:
    """Short-lived token -> long-lived (~60 day) token."""
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": META_APP_ID,
        "client_secret": META_APP_SECRET,
        "fb_exchange_token": short_lived_token,
    }
    try:
        async with create_http_client() as client:
            resp = await client.get(_endpoint("oauth/access_token"), params=params)
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta long-lived token exchange transport error: {e}")
        raise WhatsappProviderError("failed to exchange for long-lived token") from e
    if resp.status_code != 200 or "access_token" not in body:
        logger.error(f"Meta long-lived token exchange failed: {body}")
        raise WhatsappProviderError("failed to exchange for long-lived token")
    return body["access_token"]


async def get_phone_numbers(waba_id: str, access_token: str) -> List[Dict[str, Any]]:
    params = {"access_token": access_token}
    try:
        async with create_http_client() as client:
            resp = await client.get(
                _endpoint(f"{waba_id}/phone_numbers"), params=params
            )
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta phone_numbers lookup transport error: {e}")
        raise WhatsappProviderError("failed to list WABA phone numbers") from e
    if resp.status_code != 200:
        logger.error(f"Meta phone_numbers lookup failed: {body}")
        raise WhatsappProviderError("failed to list WABA phone numbers")
    return body.get("data", [])


async def verify_phone_number(
    waba_id: str, phone_number_id: str, access_token: str
) -> None:
    """Cross-checks the Embedded Signup's phone_number_id is really on this
    WABA. Raises WhatsappProviderError if it isn't — the same
    client-ID-trusting posture nautilus uses (no /debug_token call)."""
    numbers = await get_phone_numbers(waba_id, access_token)
    if not any(n.get("id") == phone_number_id for n in numbers):
        raise WhatsappProviderError(
            f"phone_number_id {phone_number_id} not found on WABA {waba_id}"
        )


async def subscribe_to_webhooks(waba_id: str, access_token: str) -> None:
    """POST /{waba_id}/subscribed_apps — subscribes this app to the WABA's
    webhook events. Confirms Meta accepted the subscription; delivery
    receipts and inbound messages still won't be processed until this repo
    has a webhook receiver for this connector (canon T11's next rung)."""
    params = {"access_token": access_token}
    try:
        async with create_http_client() as client:
            resp = await client.post(
                _endpoint(f"{waba_id}/subscribed_apps"), params=params
            )
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta webhook subscription transport error: {e}")
        raise WhatsappProviderError("failed to subscribe to webhooks") from e
    if resp.status_code != 200 or not body.get("success"):
        logger.error(f"Meta webhook subscription failed: {body}")
        raise WhatsappProviderError("failed to subscribe to webhooks")


async def register_phone_number(
    phone_number_id: str, access_token: str, pin: str
) -> None:
    """Follow-up: POST /{phone_number_id}/register (2FA PIN activation).
    Nautilus never calls this either — numbers come in pre-registered via
    Embedded Signup today."""
    raise NotImplementedError("register_phone_number: follow-up, not yet built")


async def debug_token(access_token: str) -> Dict[str, Any]:
    """Follow-up: GET /debug_token — token introspection nautilus never
    added. Verification today only confirms the phone number resolves
    (verify_phone_number), not that the token itself is well-formed/scoped."""
    raise NotImplementedError("debug_token: follow-up, not yet built")


async def create_message_template(
    waba_id: str,
    access_token: str,
    name: str,
    language: str,
    category: str,
    components: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """POST /{waba_id}/message_templates — submits a draft for review.
    Meta's response carries its own id/status/category; we store what it
    says, never what we assumed."""
    params = {"access_token": access_token}
    payload = {
        "name": name,
        "language": language,
        "category": category,
        "components": components,
    }
    try:
        async with create_http_client() as client:
            resp = await client.post(
                _endpoint(f"{waba_id}/message_templates"), params=params, json=payload
            )
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta template submission transport error: {e}")
        raise WhatsappProviderError("failed to submit template") from e
    if resp.status_code != 200 or "id" not in body:
        logger.error(f"Meta template submission failed: {body}")
        raise WhatsappProviderError("failed to submit template")
    return body


async def list_message_templates(
    waba_id: str, access_token: str
) -> List[Dict[str, Any]]:
    """GET /{waba_id}/message_templates — the periodic drift healer's
    source of truth. Follows Meta's cursor pagination (paging.next) until
    exhausted; the local registry is a snapshot, so a partial page read is
    still useful (later ticks pick up whatever was missed)."""
    params: Dict[str, Any] = {"access_token": access_token, "limit": 100}
    templates: List[Dict[str, Any]] = []
    url = _endpoint(f"{waba_id}/message_templates")
    try:
        async with create_http_client() as client:
            while url:
                resp = await client.get(url, params=params)
                body = resp.json()
                if resp.status_code != 200:
                    logger.error(f"Meta template list failed: {body}")
                    raise WhatsappProviderError("failed to list templates")
                templates.extend(body.get("data", []))
                url = body.get("paging", {}).get("next")
                params = {}
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta template list transport error: {e}")
        raise WhatsappProviderError("failed to list templates") from e
    return templates


async def edit_message_template(
    provider_template_id: str, access_token: str, components: List[Dict[str, Any]]
) -> None:
    """POST /{provider_template_id} — Meta addresses edits by the
    template's own id directly, not nested under the WABA."""
    params = {"access_token": access_token}
    payload = {"components": components}
    try:
        async with create_http_client() as client:
            resp = await client.post(
                _endpoint(provider_template_id), params=params, json=payload
            )
            body = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta template edit transport error: {e}")
        raise WhatsappProviderError("failed to edit template") from e
    if resp.status_code != 200:
        logger.error(f"Meta template edit failed: {body}")
        raise WhatsappProviderError("failed to edit template")


async def delete_message_template(waba_id: str, access_token: str, name: str) -> None:
    """DELETE /{waba_id}/message_templates?name=... — best-effort; a 404
    (already gone on Meta's side) is not our caller's problem. Every failure
    mode surfaces as WhatsappProviderError so templates.retire()'s existing
    catch keeps local retirement working when Meta is unreachable or
    returns something we can't parse."""
    params = {"access_token": access_token, "name": name}
    try:
        async with create_http_client() as client:
            resp = await client.delete(
                _endpoint(f"{waba_id}/message_templates"), params=params
            )
            if resp.status_code not in (200, 404):
                body = resp.json()
                logger.error(f"Meta template delete failed: {body}")
                raise WhatsappProviderError("failed to delete template")
    except (httpx.HTTPError, ValueError) as e:
        logger.error(f"Meta template delete transport error: {e}")
        raise WhatsappProviderError("failed to delete template") from e
