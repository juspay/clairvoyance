"""Decoder functions for Breeze Buddy data sources."""

import json
from typing import Any, List, Optional

import asyncpg

from app.schemas.breeze_buddy.data_source import (
    DataSource,
    DataSourceType,
)


def _parse_jsonb(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    return value


def decode_data_source(row: asyncpg.Record) -> DataSource:
    """Decode a data-source row."""
    return DataSource(
        id=str(row["id"]),
        reseller_id=row["reseller_id"],
        merchant_id=row["merchant_id"],
        name=row["name"],
        source_type=DataSourceType(row["source_type"]),
        config=_parse_jsonb(row["config"]) or {},
        is_active=row["is_active"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def decode_single_data_source(
    result: Optional[List[asyncpg.Record]],
) -> Optional[DataSource]:
    """Decode a single data source from query result."""
    if not result:
        return None
    return decode_data_source(result[0])


def decode_data_source_list(
    result: Optional[List[asyncpg.Record]],
) -> List[DataSource]:
    """Decode multiple data-source rows."""
    if not result:
        return []
    return [decode_data_source(row) for row in result]
