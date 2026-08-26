"""Juspay create/get customer — POST /v2/customers/{object_reference_id}."""

from typing import Any, Dict, Optional
from urllib.parse import quote

from app.services.uap import client
from app.services.uap.client import JuspayError
from app.services.uap.credentials import JuspayCredentials


async def create_or_get_customer(
    creds: JuspayCredentials,
    object_reference_id: str,
    mobile_number: str,
    mobile_country_code: str = "91",
    email_address: Optional[str] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create the customer if absent, else return the existing one.

    ``object_reference_id`` is our own id for the user: >= 8 chars, and it
    must never change — a new value mints a new customer and orphans the
    user's existing agent and mandates.

    Returns the raw response. ``id`` is the ``cst_…`` to persist;
    ``juspay.client_auth_token`` expires in 15 minutes, so pass it straight
    to the SDK and never store it.
    """
    if len(object_reference_id) < 8:
        raise ValueError("object_reference_id must be at least 8 characters")
    if not mobile_number:
        raise ValueError("mobile_number is required")

    response = await client.request(
        "POST",
        # safe="" so a reference with a slash can't escape the path.
        f"/v2/customers/{quote(object_reference_id, safe='')}",
        api_key=creds.api_key,
        merchant_id=creds.merchant_id,
        base_url=creds.base_url,
        form_body={
            "object_reference_id": object_reference_id,
            "mobile_number": mobile_number,
            "mobile_country_code": mobile_country_code,
            "email_address": email_address,
            "first_name": first_name,
            "last_name": last_name,
            # Without this the call still succeeds and returns a valid
            # customer, just with no token — the failure would surface much
            # later at the SDK call.
            "options.get_client_auth_token": True,
        },
        routing_id=object_reference_id,
    )

    if not (response.get("juspay") or {}).get("client_auth_token"):
        raise JuspayError("Juspay returned no client_auth_token", None, response)
    return response
