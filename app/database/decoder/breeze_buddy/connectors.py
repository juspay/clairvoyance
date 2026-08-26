"""Row decoders for connector records."""

import json
from typing import Any, Dict

from app.core.logger import logger
from app.schemas.breeze_buddy.connectors import (
    Connector,
    ConnectorMetric,
    ConnectorStatus,
)


def _decode_metadata(value: Any) -> Dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            logger.error("Failed to decode connector metadata JSON")
            return {}
    return dict(value) if isinstance(value, dict) else {}


def decode_connector(row) -> Connector:
    return Connector(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        connector=row["connector"],
        credential_id=str(row["credential_id"]) if row.get("credential_id") else None,
        status=ConnectorStatus(row["status"]),
        connected_at=row.get("connected_at"),
        disconnected_at=row.get("disconnected_at"),
        last_sync_at=row.get("last_sync_at"),
        metadata=_decode_metadata(row.get("metadata")),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def decode_connector_metric(row) -> ConnectorMetric:
    return ConnectorMetric(
        id=str(row["id"]),
        connector_id=str(row["connector_id"]),
        merchant_id=row["merchant_id"],
        reseller_id=row["reseller_id"],
        metric_date=row["metric_date"],
        metric_name=row["metric_name"],
        value=row["value"],
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
