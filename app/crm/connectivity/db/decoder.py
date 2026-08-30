"""Database rows -> domain shapes. Never imported outside this db package."""

from typing import Any, Mapping

from app.crm.connectivity.schemas import (
    ChannelBinding,
    ConnectorInstallation,
    QueuedMessage,
)
from app.crm.shared.decode import jsonb_object, uuid_or_none


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
