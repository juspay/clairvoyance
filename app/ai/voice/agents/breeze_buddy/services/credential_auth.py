"""Server-side resolution of credential-backed HTTP authentication."""

from typing import Any, Dict, Optional, Tuple

from pydantic import SecretStr

from app.ai.voice.agents.breeze_buddy.template.types import (
    HttpAuthConfig,
    HttpAuthType,
    TemplateModel,
)
from app.core.logger import logger
from app.database.accessor.breeze_buddy.credentials import (
    get_active_credential_by_id_for_scope,
)
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.schemas.breeze_buddy.credentials import Credential, CredentialType


def resolve_credential_scope(
    template: Optional[TemplateModel], lead: Optional[LeadCallTracker]
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve credential scope from the template, falling back to the lead."""
    reseller_id = getattr(template, "reseller_id", None)
    merchant_id = getattr(template, "merchant_id", None)

    if not reseller_id:
        reseller_id = getattr(lead, "reseller_id", None)
    if merchant_id is None:
        merchant_id = getattr(lead, "merchant_id", None)

    return reseller_id, merchant_id


async def resolve_credential_auth(
    auth_config: Optional[HttpAuthConfig],
    *,
    reseller_id: Optional[str],
    merchant_id: Optional[str],
    credential_cache: Optional[Dict[str, Credential]] = None,
) -> Optional[HttpAuthConfig]:
    """Return an auth config with a referenced credential applied in memory."""
    if auth_config is None or auth_config.credential_id is None:
        return auth_config
    if not reseller_id:
        raise ValueError("Cannot resolve credential_id without a reseller ID")

    credential_id = auth_config.credential_id
    credential = (
        credential_cache.get(credential_id) if credential_cache is not None else None
    )
    if credential is None:
        credential = await get_active_credential_by_id_for_scope(
            credential_id,
            reseller_id,
            merchant_id,
        )
        if credential and credential_cache is not None:
            credential_cache[credential_id] = credential
    if not credential or not credential.value:
        raise ValueError("Credential not found, inactive, or outside credential scope")

    credential_type = credential.credential_type
    value = credential.value
    updates: Dict[str, Any]
    if auth_config.type == HttpAuthType.BEARER:
        if credential_type != CredentialType.BEARER_TOKEN or not value.get("token"):
            raise ValueError("Bearer auth requires a bearer_token credential")
        updates = {"token": SecretStr(value["token"])}
    elif auth_config.type == HttpAuthType.API_KEY:
        if credential_type != CredentialType.API_KEY or not value.get("key"):
            raise ValueError("API key auth requires an api_key credential")
        if not auth_config.api_key_name:
            raise ValueError("API key auth requires api_key_name")
        updates = {"api_key_value": SecretStr(value["key"])}
    elif auth_config.type == HttpAuthType.BASIC:
        if (
            credential_type != CredentialType.BASIC_AUTH
            or not value.get("username")
            or not value.get("password")
        ):
            raise ValueError("Basic auth requires a basic_auth credential")
        updates = {
            "username": value["username"],
            "password": SecretStr(value["password"]),
        }
    elif auth_config.type == HttpAuthType.CUSTOM:
        if credential_type != CredentialType.CUSTOM:
            raise ValueError("Custom auth requires a custom credential")

        custom_headers = {}
        for header_name, credential_field in auth_config.header_bindings.items():
            field_value = value.get(credential_field)
            if not isinstance(field_value, str) or not field_value:
                raise ValueError(
                    "Custom auth credential is missing a non-empty field "
                    f"'{credential_field}' for header '{header_name}'"
                )
            custom_headers[header_name] = SecretStr(field_value)
        updates = {"custom_headers": custom_headers}
    else:
        raise ValueError(
            "credential_id requires bearer, basic, api_key, or custom auth"
        )

    updates["credential_id"] = None
    logger.debug(
        "Resolved credential-backed auth "
        f"credential_id={credential.id} reseller_id={reseller_id} "
        f"merchant_id={merchant_id}"
    )
    return auth_config.model_copy(update=updates)
