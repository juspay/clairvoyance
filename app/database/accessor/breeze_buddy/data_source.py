"""Database accessors for Breeze Buddy data sources."""

import json
from typing import Any, Dict, List, Optional
from uuid import uuid4

from app.core.logger import logger
from app.database.decoder.breeze_buddy.data_source import (
    decode_data_source_list,
    decode_single_data_source,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.data_source import (
    deactivate_data_source_query,
    get_data_source_by_id_query,
    insert_data_source_query,
    list_data_sources_query,
    update_data_source_query,
)
from app.schemas.breeze_buddy.data_source import (
    DataSource,
    DataSourceType,
)


async def create_data_source(
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    source_type: DataSourceType,
    config: Dict[str, Any],
) -> Optional[DataSource]:
    """Create a data source."""
    logger.info(f"Creating data source '{name}' for reseller {reseller_id}")

    try:
        query, values = insert_data_source_query(
            id=str(uuid4()),
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
            source_type=source_type.value,
            config=json.dumps(config),
        )
        result = await run_parameterized_query(query, values)
        return decode_single_data_source(result)
    except Exception as exc:
        logger.error(f"Error creating data source '{name}': {exc}", exc_info=True)
        return None


async def get_data_source_by_id(
    data_source_id: str, include_inactive: bool = False
) -> Optional[DataSource]:
    """Get a data source by ID."""
    try:
        query, values = get_data_source_by_id_query(
            data_source_id, include_inactive=include_inactive
        )
        result = await run_parameterized_query(query, values)
        return decode_single_data_source(result)
    except Exception as exc:
        logger.error(
            f"Error getting data source {data_source_id}: {exc}", exc_info=True
        )
        return None


async def list_data_sources(
    reseller_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_id: Optional[str] = None,
    merchant_ids: Optional[List[str]] = None,
    include_inactive: bool = False,
) -> List[DataSource]:
    """List data sources with optional scope filters."""
    try:
        query, values = list_data_sources_query(
            reseller_id=reseller_id,
            reseller_ids=reseller_ids,
            merchant_id=merchant_id,
            merchant_ids=merchant_ids,
            include_inactive=include_inactive,
        )
        result = await run_parameterized_query(query, values)
        return decode_data_source_list(result)
    except Exception as exc:
        logger.error(f"Error listing data sources: {exc}", exc_info=True)
        return []


async def update_data_source(
    data_source_id: str,
    name: Optional[str] = None,
    merchant_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    is_active: Optional[bool] = None,
) -> Optional[DataSource]:
    """Update a data source."""
    try:
        query, values = update_data_source_query(
            data_source_id=data_source_id,
            name=name,
            merchant_id=merchant_id,
            config=json.dumps(config) if config is not None else None,
            is_active=is_active,
        )
        result = await run_parameterized_query(query, values)
        return decode_single_data_source(result)
    except Exception as exc:
        logger.error(
            f"Error updating data source {data_source_id}: {exc}", exc_info=True
        )
        return None


async def deactivate_data_source(data_source_id: str) -> Optional[DataSource]:
    """Soft-delete a data source by marking it inactive."""
    try:
        query, values = deactivate_data_source_query(data_source_id)
        result = await run_parameterized_query(query, values)
        return decode_single_data_source(result)
    except Exception as exc:
        logger.error(
            f"Error deactivating data source {data_source_id}: {exc}", exc_info=True
        )
        return None
