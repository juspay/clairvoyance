"""
SQL query builders for the data_source table.
Layer 1 of the three-layer DB pattern: pure SQL, no DB calls.
Returns (sql_string, values_list) tuples consumed by run_parameterized_query().
"""

from datetime import datetime
from typing import Any, FrozenSet, List, Optional, Tuple

DATA_SOURCE_TABLE = "data_source"


def insert_data_source_query(
    id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    source_type: str,
    spreadsheet_url: str,
    spreadsheet_id: str,
    sheet_name: Optional[str],
    columns_json: Optional[str],
    format: str,
    is_active: bool,
    now: datetime,
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {DATA_SOURCE_TABLE}
            (id, reseller_id, merchant_id, name, source_type,
             spreadsheet_url, spreadsheet_id, sheet_name,
             columns, format, is_active, created_at, updated_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb,$10,$11,$12,$13)
        RETURNING *
    """
    return query, [
        id,
        reseller_id,
        merchant_id,
        name,
        source_type,
        spreadsheet_url,
        spreadsheet_id,
        sheet_name,
        columns_json,
        format,
        is_active,
        now,
        now,
    ]


def get_data_source_by_id_query(data_source_id: str) -> Tuple[str, List[Any]]:
    query = f"SELECT * FROM {DATA_SOURCE_TABLE} WHERE id = $1 LIMIT 1"
    return query, [data_source_id]


def list_data_sources_query(
    page: int,
    limit: int,
    reseller_id: Optional[str] = None,
    reseller_ids: Optional[List[str]] = None,
    merchant_id: Optional[str] = None,
    is_active: Optional[bool] = None,
) -> Tuple[str, str, List[Any]]:
    """Returns (data_query, count_query, values). count_query has same WHERE, no LIMIT/OFFSET."""
    conditions: List[str] = []
    values: List[Any] = []

    if reseller_ids:
        values.append(reseller_ids)
        conditions.append(f"reseller_id = ANY(${len(values)})")
    elif reseller_id:
        values.append(reseller_id)
        conditions.append(f"reseller_id = ${len(values)}")

    if merchant_id:
        values.append(merchant_id)
        conditions.append(f"merchant_id = ${len(values)}")

    if is_active is not None:
        values.append(is_active)
        conditions.append(f"is_active = ${len(values)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    offset = (page - 1) * limit

    data_query = f"""
        SELECT * FROM {DATA_SOURCE_TABLE}
        {where}
        ORDER BY created_at DESC
        LIMIT ${len(values) + 1} OFFSET ${len(values) + 2}
    """
    count_query = f"SELECT COUNT(*) AS total FROM {DATA_SOURCE_TABLE} {where}"

    return data_query, count_query, values + [limit, offset]


def update_data_source_query(
    data_source_id: str,
    update_fields: FrozenSet[str],
    name: Optional[str],
    spreadsheet_url: Optional[str],
    spreadsheet_id: Optional[str],
    sheet_name: Optional[str],
    columns_json: Optional[str],
    format: Optional[str],
    is_active: Optional[bool],
    now: datetime,
) -> Tuple[str, List[Any]]:
    sets: List[str] = []
    values: List[Any] = []

    def _add_if(col: str, val: Any, cast: str = "") -> None:
        if col in update_fields:
            sets.append(f"{col} = ${len(values) + 1}{cast}")
            values.append(val)

    _add_if("name", name)
    _add_if("spreadsheet_url", spreadsheet_url)
    _add_if("spreadsheet_id", spreadsheet_id)
    _add_if("sheet_name", sheet_name)
    _add_if("columns", columns_json, "::jsonb")
    _add_if("format", format)
    _add_if("is_active", is_active)

    sets.append(f"updated_at = ${len(values) + 1}")
    values.append(now)
    values.append(data_source_id)

    query = f"""
        UPDATE {DATA_SOURCE_TABLE}
        SET {", ".join(sets)}
        WHERE id = ${len(values)}
        RETURNING *
    """
    return query, values


def delete_data_source_query(data_source_id: str) -> Tuple[str, List[Any]]:
    query = f"DELETE FROM {DATA_SOURCE_TABLE} WHERE id = $1 RETURNING id"
    return query, [data_source_id]
