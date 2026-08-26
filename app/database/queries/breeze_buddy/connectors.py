"""SQL builders for generic merchant connectors and their metrics."""

import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

CONNECTORS_TABLE = "connectors"
CONNECTOR_METRICS_TABLE = "connector_metrics"
CONNECTOR_CREDENTIAL_PREFIX = "connector:"

_CONNECTOR_COLUMNS = (
    "id, reseller_id, merchant_id, connector, credential_id, status, connected_at, "
    "disconnected_at, last_sync_at, metadata, created_at, updated_at"
)
_METRIC_COLUMNS = (
    "id, connector_id, merchant_id, reseller_id, metric_date, metric_name, value, "
    "created_at, updated_at"
)


def connector_credential_name(connector: str) -> str:
    return f"{CONNECTOR_CREDENTIAL_PREFIX}{connector}"


def upsert_connector_credential_query(
    *,
    credential_id: str,
    reseller_id: str,
    merchant_id: str,
    connector: str,
    value: str,
    is_encrypted: bool,
    description: Optional[str],
) -> Tuple[str, List[Any]]:
    now = datetime.now(timezone.utc)
    query = """
        INSERT INTO credentials
            (id, reseller_id, merchant_id, name, credential_type, value,
             is_encrypted, description, is_active, template_exposable,
             created_at, updated_at)
        VALUES ($1::uuid, $2, $3, $4, 'custom', $5, $6, $7, TRUE, FALSE, $8, $8)
        ON CONFLICT (reseller_id, merchant_id, name)
        WHERE is_active = TRUE
          AND template_exposable = FALSE
          AND reseller_id IS NOT NULL
          AND merchant_id IS NOT NULL
        DO UPDATE SET
            value = EXCLUDED.value,
            is_encrypted = EXCLUDED.is_encrypted,
            description = EXCLUDED.description,
            updated_at = EXCLUDED.updated_at
        RETURNING *;
    """
    return (
        query,
        [
            credential_id,
            reseller_id,
            merchant_id,
            connector_credential_name(connector),
            value,
            is_encrypted,
            description,
            now,
        ],
    )


def get_active_connector_credential_query(
    *, reseller_id: str, merchant_id: str, connector: str, credential_id: str
) -> Tuple[str, List[Any]]:
    query = """
        SELECT * FROM credentials
        WHERE id = $1::uuid
          AND reseller_id = $2
          AND merchant_id = $3
          AND name = $4
          AND is_active = TRUE
          AND template_exposable = FALSE;
    """
    return query, [
        credential_id,
        reseller_id,
        merchant_id,
        connector_credential_name(connector),
    ]


def upsert_connector_query(
    *,
    reseller_id: str,
    merchant_id: str,
    connector: str,
    credential_id: str,
    metadata: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    now = datetime.now(timezone.utc)
    query = f"""
        INSERT INTO {CONNECTORS_TABLE}
            (reseller_id, merchant_id, connector, credential_id, status,
             connected_at, disconnected_at, last_sync_at, metadata, created_at,
             updated_at)
        VALUES ($1, $2, $3, $4::uuid, 'connected', $5, NULL, $5, $6::jsonb, $5, $5)
        ON CONFLICT (reseller_id, merchant_id, connector)
        DO UPDATE SET
            credential_id = EXCLUDED.credential_id,
            status = 'connected',
            connected_at = CASE
                WHEN {CONNECTORS_TABLE}.status IN ('disconnected', 'error')
                THEN EXCLUDED.connected_at
                ELSE {CONNECTORS_TABLE}.connected_at
            END,
            disconnected_at = NULL,
            last_sync_at = EXCLUDED.last_sync_at,
            metadata = EXCLUDED.metadata,
            updated_at = EXCLUDED.updated_at
        RETURNING {_CONNECTOR_COLUMNS};
    """
    return query, [
        reseller_id,
        merchant_id,
        connector,
        credential_id,
        now,
        json.dumps(metadata or {}),
    ]


def get_connector_query(
    reseller_id: str, merchant_id: str, connector: str, *, active_only: bool = False
) -> Tuple[str, List[Any]]:
    status_filter = "AND status = 'connected'" if active_only else ""
    query = f"""
        SELECT {_CONNECTOR_COLUMNS}
        FROM {CONNECTORS_TABLE}
        WHERE reseller_id = $1 AND merchant_id = $2 AND connector = $3
        {status_filter};
    """
    return query, [reseller_id, merchant_id, connector]


def disconnect_connector_query(
    reseller_id: str, merchant_id: str, connector: str
) -> Tuple[str, List[Any]]:
    now = datetime.now(timezone.utc)
    query = f"""
        WITH disconnected_connector AS (
            UPDATE {CONNECTORS_TABLE}
            SET status = 'disconnected', disconnected_at = $4, updated_at = $4
            WHERE reseller_id = $1 AND merchant_id = $2 AND connector = $3
            RETURNING credential_id
        ), deactivated_credential AS (
            UPDATE credentials c
            SET is_active = FALSE, updated_at = $4
            FROM disconnected_connector dc
            WHERE c.id = dc.credential_id
            RETURNING c.id
        )
        SELECT credential_id FROM disconnected_connector;
    """
    return query, [reseller_id, merchant_id, connector, now]


def mark_connector_error_query(
    reseller_id: str, merchant_id: str, connector: str
) -> Tuple[str, List[Any]]:
    now = datetime.now(timezone.utc)
    query = f"""
        UPDATE {CONNECTORS_TABLE}
        SET status = 'error', updated_at = $4
        WHERE reseller_id = $1 AND merchant_id = $2 AND connector = $3
        RETURNING {_CONNECTOR_COLUMNS};
    """
    return query, [reseller_id, merchant_id, connector, now]


def increment_connector_metric_query(
    *,
    connector_id: str,
    reseller_id: str,
    merchant_id: str,
    metric_name: str,
    increment: int,
    metric_date: date,
) -> Tuple[str, List[Any]]:
    now = datetime.now(timezone.utc)
    query = f"""
        INSERT INTO {CONNECTOR_METRICS_TABLE}
            (connector_id, reseller_id, merchant_id, metric_date, metric_name,
             value, created_at, updated_at)
        VALUES ($1::uuid, $2, $3, $4, $5, $6, $7, $7)
        ON CONFLICT (connector_id, metric_date, metric_name)
        DO UPDATE SET value = {CONNECTOR_METRICS_TABLE}.value + EXCLUDED.value,
                      updated_at = EXCLUDED.updated_at
        RETURNING {_METRIC_COLUMNS};
    """
    return query, [
        connector_id,
        reseller_id,
        merchant_id,
        metric_date,
        metric_name,
        increment,
        now,
    ]


def get_connector_metrics_query(
    *,
    connector_id: str,
    reseller_id: str,
    merchant_id: str,
    start_date: date,
    end_date: date,
    metric_name: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    values: List[Any] = [connector_id, reseller_id, merchant_id, start_date, end_date]
    metric_filter = ""
    if metric_name:
        metric_filter = "AND metric_name = $6"
        values.append(metric_name)
    query = f"""
        SELECT {_METRIC_COLUMNS}
        FROM {CONNECTOR_METRICS_TABLE}
        WHERE connector_id = $1::uuid
          AND reseller_id = $2
          AND merchant_id = $3
          AND metric_date BETWEEN $4 AND $5
          {metric_filter}
        ORDER BY metric_date ASC, metric_name ASC;
    """
    return query, values
