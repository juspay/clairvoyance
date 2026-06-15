"""
Accessor functions for the data_source table.
Layer 2 of the three-layer DB pattern: business logic + DB execution.
"""

import json
from datetime import datetime, timezone
from typing import List, Optional, Tuple
from uuid import uuid4

from app.core.logger import logger
from app.database.decoder.breeze_buddy.data_source import (
    decode_data_source,
    decode_data_sources,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.data_source import (
    delete_data_source_query,
    get_data_source_by_id_query,
    insert_data_source_query,
    list_data_sources_query,
    update_data_source_query,
)
from app.schemas.breeze_buddy.data_source import DataSourceResponse
from app.services.google.sheets import extract_spreadsheet_id


async def create_data_source(
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    source_type: str,
    spreadsheet_url: str,
    sheet_name: Optional[str],
    columns: Optional[List[str]],
    format: str,
    is_active: bool = True,
) -> Optional[DataSourceResponse]:
    """Create a new data source record."""
    try:
        spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
        if not spreadsheet_id:
            logger.error(f"Cannot extract spreadsheet_id from URL: {spreadsheet_url}")
            return None

        now = datetime.now(timezone.utc)
        columns_json = json.dumps(columns) if columns else None

        query, values = insert_data_source_query(
            id=str(uuid4()),
            reseller_id=reseller_id,
            merchant_id=merchant_id,
            name=name,
            source_type=source_type,
            spreadsheet_url=spreadsheet_url,
            spreadsheet_id=spreadsheet_id,
            sheet_name=sheet_name,
            columns_json=columns_json,
            format=format,
            is_active=is_active,
            now=now,
        )
        result = await run_parameterized_query(query, values)
        return decode_data_source(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error creating data_source: {e}", exc_info=True)
        return None


async def get_data_source_by_id(
    data_source_id: str,
) -> Optional[DataSourceResponse]:
    """Fetch a single data source by ID."""
    try:
        query, values = get_data_source_by_id_query(data_source_id)
        result = await run_parameterized_query(query, values)
        return decode_data_source(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error fetching data_source {data_source_id}: {e}", exc_info=True)
        return None


async def list_data_sources(
    page: int = 1,
    limit: int = 50,
    reseller_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_id: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[List[DataSourceResponse], int]:
    """List data sources with pagination and optional filters."""
    try:
        data_q, count_q, values = list_data_sources_query(
            page=page,
            limit=limit,
            reseller_id=reseller_id,
            reseller_ids=reseller_ids,
            merchant_id=merchant_id,
            is_active=is_active,
        )
        rows = await run_parameterized_query(data_q, values)
        # count_query uses same WHERE — strip LIMIT/OFFSET values (last 2)
        count_result = await run_parameterized_query(count_q, values[:-2])
        total = count_result[0]["total"] if count_result else 0
        return decode_data_sources(rows or []), total
    except Exception as e:
        logger.error(f"Error listing data_sources: {e}", exc_info=True)
        return [], 0


async def update_data_source(
    data_source_id: str,
    name: Optional[str] = None,
    spreadsheet_url: Optional[str] = None,
    sheet_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
    format: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Optional[DataSourceResponse]:
    """Update an existing data source. Only provided fields are updated."""
    try:
        new_spreadsheet_id = None
        if spreadsheet_url:
            new_spreadsheet_id = extract_spreadsheet_id(spreadsheet_url)
            if not new_spreadsheet_id:
                logger.error(
                    f"Cannot extract spreadsheet_id from URL: {spreadsheet_url}"
                )
                return None

        now = datetime.now(timezone.utc)
        columns_json = json.dumps(columns) if columns is not None else None

        query, values = update_data_source_query(
            data_source_id=data_source_id,
            name=name,
            spreadsheet_url=spreadsheet_url,
            spreadsheet_id=new_spreadsheet_id,
            sheet_name=sheet_name,
            columns_json=columns_json,
            format=format,
            is_active=is_active,
            now=now,
        )
        result = await run_parameterized_query(query, values)
        return decode_data_source(result[0]) if result else None
    except Exception as e:
        logger.error(f"Error updating data_source {data_source_id}: {e}", exc_info=True)
        return None


async def delete_data_source(data_source_id: str) -> bool:
    """Hard delete a data source. Returns True if deleted."""
    try:
        query, values = delete_data_source_query(data_source_id)
        result = await run_parameterized_query(query, values)
        return bool(result)
    except Exception as e:
        logger.error(f"Error deleting data_source {data_source_id}: {e}", exc_info=True)
        return False
