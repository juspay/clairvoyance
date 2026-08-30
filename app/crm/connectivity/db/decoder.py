"""Row -> domain shapes for the connectivity module (module rules §1).
DB-side translation only — never imported outside db/.

Two families on purpose. The send path reads ConnectorInstallation /
ChannelBinding: the minimum a sender needs, credential_id included, no
timestamps. The API surface reads InstallationRead / ChannelBindingRead:
what the console renders — health_detail (canon T11 mandates it below
'healthy'), the timestamps, and deliberately NOT credential_id, which
names a vault row and has no business on a response body.
"""

from typing import Any, Mapping

from app.crm.connectivity.schemas import (
    ChannelBinding,
    ChannelBindingRead,
    ConnectorInstallation,
    InstallationRead,
    QueuedMessage,
    TemplateRead,
)
from app.crm.shared.decode import jsonb_list, jsonb_object, uuid_or_none


def decode_queued_message(row: Mapping[str, Any]) -> QueuedMessage:
    """One claimed crm_message row -> QueuedMessage."""
    return QueuedMessage(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        customer_id=str(row["customer_id"]),
        channel=row["channel"],
        sent_to_address=row["sent_to_address"],
        binding_id=uuid_or_none(row["binding_id"]),
        source_kind=row["source_kind"],
        source_id=uuid_or_none(row["source_id"]),
        purpose_key=row["purpose_key"],
        template_id=row["template_id"],
        # Totality matters most here: a whole batch is decoded after its claim
        # commits but outside the per-message error handling, so one raise
        # would strand every row in it — permanently (see shared/decode.py).
        variables=jsonb_object(row["variables"]),
        dedupe_key=row["dedupe_key"],
        attempt=row["attempt"],
        next_attempt_at=row["next_attempt_at"],
    )


def decode_installation(row: Mapping[str, Any]) -> ConnectorInstallation:
    """One crm_connector_installation row -> ConnectorInstallation."""
    return ConnectorInstallation(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        connector_key=row["connector_key"],
        external_account_id=row["external_account_id"],
        display_label=row["display_label"],
        credential_id=uuid_or_none(row["credential_id"]),
        status=row["status"],
        token_expires_at=row["token_expires_at"],
    )


def decode_binding(row: Mapping[str, Any]) -> ChannelBinding:
    """One crm_channel_binding row -> ChannelBinding."""
    return ChannelBinding(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        installation_id=str(row["installation_id"]),
        address=row["address"],
        # An unreadable capabilities blob must not refuse a send: an empty
        # dict means "declares nothing", which every caller already handles.
        capabilities=jsonb_object(row["capabilities"]),
        is_primary=row["is_primary"],
        status=row["status"],
    )


def decode_installation_read(row: Mapping[str, Any]) -> InstallationRead:
    """The same row -> the console's shape. No credential_id."""
    return InstallationRead(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        connector_key=row["connector_key"],
        external_account_id=row["external_account_id"],
        display_label=row["display_label"],
        status=row["status"],
        token_expires_at=row["token_expires_at"],
        last_event_at=row["last_event_at"],
        health_detail=jsonb_object(row["health_detail"]),
        installed_at=row["installed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_binding_read(row: Mapping[str, Any]) -> ChannelBindingRead:
    """The same row -> the console's shape, with the timestamps."""
    return ChannelBindingRead(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        installation_id=str(row["installation_id"]),
        address=row["address"],
        capabilities=jsonb_object(row["capabilities"]),
        is_primary=row["is_primary"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_template(row: Mapping[str, Any]) -> TemplateRead:
    """One crm_template row -> TemplateRead."""
    return TemplateRead(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        channel=row["channel"],
        provider_account_ref=row["provider_account_ref"],
        name=row["name"],
        language=row["language"],
        provider_template_id=row["provider_template_id"],
        category=row["category"],
        submitted_category=row["submitted_category"],
        category_updated_at=row["category_updated_at"],
        components=jsonb_list(row["components"]),
        status=row["status"],
        status_updated_at=row["status_updated_at"],
        rejection_reason=row["rejection_reason"],
        quality=row["quality"],
        quality_updated_at=row["quality_updated_at"],
        last_synced_at=row["last_synced_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
