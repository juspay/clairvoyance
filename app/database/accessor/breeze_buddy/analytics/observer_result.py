"""Database access for observer analytics over evaluation_result."""

from typing import Any, Dict, List

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.analytics.observer_result import (
    get_legacy_observer_detection_rows_query,
    get_observer_aggregate_rows_query,
    get_observer_detection_rows_query,
    get_observer_eligible_conversation_count_query,
)


async def get_observer_detection_rows_from_db(
    filters: Dict[str, Any],
    limit: int = 10000,
) -> List[Dict[str, Any]]:
    try:
        query, values = get_observer_detection_rows_query(filters, limit=limit)
        rows = await run_parameterized_query(query, values)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching observer detection rows: {e}")
        raise


async def get_legacy_observer_detection_rows_from_db(
    filters: Dict[str, Any],
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    try:
        query, values = get_legacy_observer_detection_rows_query(filters, limit=limit)
        rows = await run_parameterized_query(query, values)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error fetching legacy observer detection rows: {e}")
        raise


async def get_observer_eligible_conversation_count_from_db(
    filters: Dict[str, Any],
) -> int:
    try:
        query, values = get_observer_eligible_conversation_count_query(filters)
        rows = await run_parameterized_query(query, values)
        return int(rows[0]["total"]) if rows else 0
    except Exception as e:
        logger.error(f"Error counting observer-eligible conversations: {e}")
        raise


async def get_observer_aggregate_rows_from_db(
    filters: Dict[str, Any],
) -> List[Dict[str, Any]]:
    try:
        query, values = get_observer_aggregate_rows_query(filters)
        rows = await run_parameterized_query(query, values)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error(f"Error aggregating observer detections: {e}")
        raise
