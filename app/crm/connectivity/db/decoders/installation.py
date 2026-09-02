"""crm_connector_installation rows -> domain shapes.

Two decoders for two column lists, and the split is the point: the route
shape carries ``credential_id`` because send.py must follow it into the
vault; the read shape does not, because an API response naming where a
secret lives is a map to it. A single decoder would force one of those two
to be wrong.
"""

from typing import Any, Mapping

from app.crm.connectivity.schemas.connector import (
    ConnectorInstallation,
    InstallationRead,
)
from app.crm.shared.decode import jsonb_object, uuid_or_none


def decode_installation(row: Mapping[str, Any]) -> ConnectorInstallation:
    """The route shape — what send.py needs to reach a provider."""
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


def decode_installation_read(row: Mapping[str, Any]) -> InstallationRead:
    """The console shape — the health story, no pointer to a secret."""
    return InstallationRead(
        id=str(row["id"]),
        merchant_id=row["merchant_id"],
        connector_key=row["connector_key"],
        external_account_id=row["external_account_id"],
        display_label=row["display_label"],
        status=row["status"],
        token_expires_at=row["token_expires_at"],
        last_event_at=row["last_event_at"],
        # Total: an unreadable health blob must render as "nothing declared",
        # not as a 500 on the connections screen.
        health_detail=jsonb_object(row["health_detail"]),
        installed_at=row["installed_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
