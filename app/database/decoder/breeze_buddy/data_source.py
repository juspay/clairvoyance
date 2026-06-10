"""
Decoder for the data_source table.
Converts raw asyncpg Records into DataSourceResponse Pydantic models.
Layer 3 of the three-layer DB pattern.
"""

import json
from typing import List, Optional

import asyncpg

from app.schemas.breeze_buddy.data_source import DataSourceResponse


def decode_data_source(result: asyncpg.Record) -> Optional[DataSourceResponse]:
    """Decode a single data_source row."""
    if not result:
        return None

    columns = result.get("columns")
    if columns and isinstance(columns, str):
        columns = json.loads(columns)

    return DataSourceResponse(
        id=str(result["id"]),
        reseller_id=result["reseller_id"],
        merchant_id=result.get("merchant_id"),
        name=result["name"],
        source_type=result["source_type"],
        spreadsheet_url=result["spreadsheet_url"],
        spreadsheet_id=result["spreadsheet_id"],
        sheet_name=result.get("sheet_name"),
        columns=columns,
        format=result["format"],
        is_active=result["is_active"],
        created_at=result["created_at"],
        updated_at=result["updated_at"],
    )


def decode_data_sources(results: List[asyncpg.Record]) -> List[DataSourceResponse]:
    """Decode a list of data_source rows."""
    if not results:
        return []
    decoded = [decode_data_source(r) for r in results]
    return [d for d in decoded if d is not None]
