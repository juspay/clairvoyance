"""
Secrets masking utilities for template API responses.

These utilities ensure sensitive data (API keys, tokens, passwords) are never
exposed in API responses while allowing updates to preserve existing values.
"""

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
from app.core.logger import logger

# Mask value for top-level template.secrets in API responses (legacy: 6 asterisks).
SECRETS_MASK = "******"

# Mask values that ``configurations.mcp.servers[*].auth.{token,password,
# api_key_value}`` may arrive with on a PUT body. ``HttpAuthConfig``'s
# field_serializer in ``template/types.py`` emits 10 asterisks; accept the
# 6-asterisk top-level form too in case a UI client normalises everything to
# one shape. Either form means "value unchanged — keep what's in DB".
_AUTH_MASK_VALUES = frozenset({SECRETS_MASK, "**********"})

# The three SecretStr fields on ``HttpAuthConfig``. If the schema gains
# another secret-typed auth field, add it here.
_AUTH_SECRET_FIELDS: tuple[str, ...] = ("token", "password", "api_key_value")


def mask_secrets(secrets: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    Mask all secret values with **** for API responses.

    Args:
        secrets: Dictionary of secrets or None

    Returns:
        Dictionary with all values replaced with ****, or None if input is None
    """
    if not secrets:
        return None
    return {key: SECRETS_MASK for key in secrets.keys()}


def merge_secrets(
    incoming_secrets: Optional[Dict[str, Any]],
    existing_secrets: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """
    Merge incoming secrets with existing secrets, preserving masked values.

    Handles these cases:
    - If incoming value is "****", keep existing DB value
    - If incoming value is something else, use the new value
    - If key is removed from incoming (not present), remove it
    - If key is new in incoming, add it

    Args:
        incoming_secrets: Secrets from the update request (may contain ****)
        existing_secrets: Current secrets from database (real values)

    Returns:
        Merged secrets dictionary with real values
    """
    if not incoming_secrets:
        return None

    existing = existing_secrets or {}
    merged = {}

    for key, value in incoming_secrets.items():
        if value == SECRETS_MASK:
            # Keep existing value if it exists, otherwise skip this key
            if key in existing:
                merged[key] = existing[key]
            # If key doesn't exist in DB but UI sends ****, skip it
        else:
            # Use the new value
            merged[key] = value

    return merged if merged else None


def merge_masked_mcp_auth(
    new_configurations: Optional[Dict[str, Any]],
    existing_configurations: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """In-place merge: replace masked auth secrets in ``new_configurations``
    with the existing persisted values. Mirrors ``merge_secrets`` for
    top-level template.secrets, but for the SecretStr fields under
    ``configurations.mcp.servers[*].auth``.

    Why this is needed: ``HttpAuthConfig.token``/``password``/``api_key_value``
    are SecretStr fields. ``GET /templates/{id}`` masks them in the response
    (Pydantic field_serializer, no reveal_secrets context). A client that does
    GET → edit one unrelated field → PUT will send the masked literal back; if
    we persist that literal, runtime sends ``Authorization: Bearer **********``
    and auth silently breaks. Treat masked values as "unchanged" and substitute
    the existing persisted value.

    Pairing strategy: prefer ``server.name`` (the natural identity), fall back
    to position. If neither matches, the masked value is left as-is and a
    warning is logged — caller is responsible for either supplying a real
    value or matching an existing server.

    Currently only walks ``configurations.mcp.servers[*].auth`` because that's
    the only HttpAuthConfig location reachable through ``ConfigurationModel``.
    If the schema gains another auth-bearing path under configurations, extend
    this walker.
    """
    if not new_configurations or not existing_configurations:
        return new_configurations

    new_servers = ((new_configurations.get("mcp") or {}).get("servers")) or []
    existing_servers = ((existing_configurations.get("mcp") or {}).get("servers")) or []
    if not new_servers:
        return new_configurations

    # Index existing servers by name (skip duplicates and unnamed entries —
    # those fall through to position-based pairing).
    seen_names: set = set()
    by_name: Dict[str, Dict[str, Any]] = {}
    for s in existing_servers:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not isinstance(name, str):
            continue
        if name in seen_names:
            by_name.pop(name, None)  # ambiguous — disqualify
            continue
        seen_names.add(name)
        by_name[name] = s

    for i, new_server in enumerate(new_servers):
        if not isinstance(new_server, dict):
            continue
        new_auth = new_server.get("auth")
        if not isinstance(new_auth, dict):
            continue

        # Find the existing pair: by name first, then by position.
        existing_server: Optional[Dict[str, Any]] = None
        name = new_server.get("name")
        if isinstance(name, str) and name in by_name:
            existing_server = by_name[name]
        elif i < len(existing_servers) and isinstance(existing_servers[i], dict):
            existing_server = existing_servers[i]

        existing_auth = (
            existing_server.get("auth") if existing_server is not None else None
        )

        for field in _AUTH_SECRET_FIELDS:
            v = new_auth.get(field)
            if not (isinstance(v, str) and v in _AUTH_MASK_VALUES):
                continue
            existing_v = (
                existing_auth.get(field) if isinstance(existing_auth, dict) else None
            )
            if isinstance(existing_v, str):
                new_auth[field] = existing_v
            else:
                # Masked value with no real counterpart — most likely a
                # newly-added MCP server with a placeholder the caller forgot
                # to fill, or a renamed server that broke the pairing. Don't
                # silently persist the literal mask: clear the field so the
                # template fails loudly at MCP setup rather than appearing
                # configured but sending ``Bearer **********``.
                logger.warning(
                    "merge_masked_mcp_auth: masked %r on mcp server %r has no "
                    "existing value to substitute; clearing the field",
                    field,
                    name or f"<index={i}>",
                )
                new_auth[field] = None

    return new_configurations


def mask_template_secrets(template: TemplateModel) -> TemplateModel:
    """
    Return a copy of the template with secrets masked for API responses.

    Args:
        template: TemplateModel with potentially sensitive secrets

    Returns:
        TemplateModel with secrets values replaced with ****
    """
    # Create a copy with masked secrets
    return TemplateModel(
        id=template.id,
        reseller_id=template.reseller_id,
        merchant_id=template.merchant_id,
        name=template.name,
        flow=template.flow,
        expected_payload_schema=template.expected_payload_schema,
        expected_callback_response_schema=template.expected_callback_response_schema,
        configurations=template.configurations,
        secrets=mask_secrets(template.secrets),
        outbound_number_id=template.outbound_number_id,
        is_active=template.is_active,
        # ``supported_channels`` must be carried through the masked copy.
        # If not preserved, GET / PUT response paths fall back to the
        # ``TemplateModel`` default (``['voice']``) and chat-enabled
        # templates appear voice-only to API clients — which then PUT the
        # default back, silently disabling chat on round-trip edits.
        supported_channels=list(template.supported_channels),
        data_sources=template.data_sources,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )
