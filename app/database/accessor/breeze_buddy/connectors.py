"""Async accessors for generic merchant connectors and daily metrics."""

from datetime import date, datetime, timezone
from typing import List, Optional
from uuid import uuid4

from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.connectors import (
    decode_connector,
    decode_connector_metric,
)
from app.database.decoder.breeze_buddy.credentials import decode_credential
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.connectors import (
    disconnect_connector_query,
    get_active_connector_credential_query,
    get_connector_metrics_query,
    get_connector_query,
    increment_connector_metric_query,
    mark_connector_error_query,
    upsert_connector_credential_query,
    upsert_connector_query,
)
from app.schemas import Credential
from app.schemas.breeze_buddy.connectors import (
    Connector,
    ConnectorMetric,
    ConnectorMetricIncrement,
    UpsertConnectorConnection,
)


async def get_connector(
    reseller_id: str, merchant_id: str, connector: str
) -> Optional[Connector]:
    try:
        query, values = get_connector_query(reseller_id, merchant_id, connector)
        result = await run_parameterized_query(query, values)
        return decode_connector(result[0]) if result else None
    except Exception as e:
        logger.error(
            f"Error fetching connector {connector} for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        return None


async def get_active_connector(
    reseller_id: str, merchant_id: str, connector: str
) -> Optional[Connector]:
    try:
        query, values = get_connector_query(
            reseller_id, merchant_id, connector, active_only=True
        )
        result = await run_parameterized_query(query, values)
        return decode_connector(result[0]) if result else None
    except Exception as e:
        logger.error(
            f"Error fetching active connector {connector} for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        return None


async def sync_connector_connection(
    connection: UpsertConnectorConnection,
) -> Optional[Connector]:
    """Atomically create/update a connector credential and connector state."""
    try:
        credential_query, credential_values = upsert_connector_credential_query(
            credential_id=str(uuid4()),
            reseller_id=connection.reseller_id,
            merchant_id=connection.merchant_id,
            connector=connection.connector,
            value=connection.credential_value,
            is_encrypted=connection.credential_is_encrypted,
            description=connection.credential_description,
        )

        async for conn in get_db_connection():
            async with conn.transaction():
                credential_rows = await conn.fetch(credential_query, *credential_values)
                if not credential_rows:
                    raise RuntimeError("Connector credential upsert returned no row")

                connector_query, connector_values = upsert_connector_query(
                    reseller_id=connection.reseller_id,
                    merchant_id=connection.merchant_id,
                    connector=connection.connector,
                    credential_id=str(credential_rows[0]["id"]),
                    metadata=connection.metadata,
                )
                connector_rows = await conn.fetch(connector_query, *connector_values)

            if connector_rows:
                return decode_connector(connector_rows[0])
            return None
    except Exception as e:
        logger.error(
            f"Error syncing connector {connection.connector} for "
            f"reseller={connection.reseller_id} merchant={connection.merchant_id}: {e}",
            exc_info=True,
        )
        return None


async def disconnect_connector(
    reseller_id: str, merchant_id: str, connector: str
) -> bool:
    try:
        query, values = disconnect_connector_query(reseller_id, merchant_id, connector)
        return bool(await run_parameterized_query(query, values))
    except Exception as e:
        logger.error(
            f"Error disconnecting connector {connector} for reseller={reseller_id} "
            f"merchant={merchant_id}: {e}"
        )
        return False


async def mark_connector_error(
    reseller_id: str, merchant_id: str, connector: str
) -> Optional[Connector]:
    try:
        query, values = mark_connector_error_query(reseller_id, merchant_id, connector)
        result = await run_parameterized_query(query, values)
        return decode_connector(result[0]) if result else None
    except Exception as e:
        logger.error(
            f"Error marking connector {connector} as error for "
            f"reseller={reseller_id} merchant={merchant_id}: {e}"
        )
        return None


async def get_active_connector_credential(
    *, reseller_id: str, merchant_id: str, connector: str, credential_id: str
) -> Optional[Credential]:
    """Fetch one scoped, non-template-exposable connector credential."""
    try:
        query, values = get_active_connector_credential_query(
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            connector=connector,
            credential_id=credential_id,
        )
        result = await run_parameterized_query(query, values)
        return decode_credential(result[0], mask=False) if result else None
    except Exception as e:
        logger.error(
            f"Error fetching connector credential for {connector}, "
            f"reseller={reseller_id} merchant={merchant_id}: {e}"
        )
        return None


async def increment_connector_metric(
    metric: ConnectorMetricIncrement,
) -> Optional[ConnectorMetric]:
    if metric.increment == 0:
        return None
    try:
        query, values = increment_connector_metric_query(
            connector_id=metric.connector_id,
            reseller_id=metric.reseller_id,
            merchant_id=metric.merchant_id,
            metric_name=metric.metric_name,
            increment=metric.increment,
            metric_date=metric.metric_date or datetime.now(timezone.utc).date(),
        )
        result = await run_parameterized_query(query, values)
        return decode_connector_metric(result[0]) if result else None
    except Exception as e:
        logger.error(
            f"Error incrementing connector metric {metric.metric_name} for "
            f"connector={metric.connector_id}: {e}"
        )
        return None


async def get_connector_metrics(
    *,
    connector_id: str,
    reseller_id: str,
    merchant_id: str,
    start_date: date,
    end_date: date,
    metric_name: Optional[str] = None,
) -> List[ConnectorMetric]:
    try:
        query, values = get_connector_metrics_query(
            connector_id=connector_id,
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            start_date=start_date,
            end_date=end_date,
            metric_name=metric_name,
        )
        result = await run_parameterized_query(query, values)
        return [decode_connector_metric(row) for row in result]
    except Exception as e:
        logger.error(f"Error fetching connector metrics for {connector_id}: {e}")
        return []
