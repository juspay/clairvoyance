"""
Secrets masking utilities for template API responses.

These utilities ensure sensitive data (API keys, tokens, passwords) are never
exposed in API responses while allowing updates to preserve existing values.
"""

from typing import Any, Dict, Optional

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

# Mask value for secrets in API responses
SECRETS_MASK = "******"


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
        merchant_id=template.merchant_id,
        shop_identifier=template.shop_identifier,
        name=template.name,
        flow=template.flow,
        expected_payload_schema=template.expected_payload_schema,
        expected_callback_response_schema=template.expected_callback_response_schema,
        configurations=template.configurations,
        secrets=mask_secrets(template.secrets),
        outbound_number_id=template.outbound_number_id,
        is_active=template.is_active,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )
