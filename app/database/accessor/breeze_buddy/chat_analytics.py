"""Accessors for chat aggregate analytics (Analytics → Chats dashboard).

Executes the chat_analytics query builders and returns plain row dicts the
handler shapes into the response (mirrors the voice analytics accessor).
"""

from typing import Any, Dict, List, Optional

from app.core.logger import logger
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.chat_analytics import (
    get_chat_analytics_summary_query,
    get_chat_analytics_trends_query,
    get_chats_by_hour_query,
)


async def get_chat_summary_from_db(
    filters: Dict[str, Any], group_by: Optional[str] = None
) -> List[Dict[str, Any]]:
    """Aggregate chat metrics. Single-row list when not grouped; one row per
    agent when ``group_by='template'``."""
    query, values = get_chat_analytics_summary_query(filters, group_by)
    try:
        rows = await run_parameterized_query(query, values)
        return [dict(row) for row in rows] if rows else []
    except Exception as e:
        logger.error(f"Error fetching chat analytics summary: {e}", exc_info=True)
        raise


async def get_chat_trends_from_db(
    filters: Dict[str, Any], time_granularity: str = "day"
) -> List[Dict[str, Any]]:
    """Time-bucketed chats-started series."""
    query, values = get_chat_analytics_trends_query(filters, time_granularity)
    try:
        rows = await run_parameterized_query(query, values)
        return [dict(row) for row in rows] if rows else []
    except Exception as e:
        logger.error(f"Error fetching chat analytics trends: {e}", exc_info=True)
        raise


async def get_chats_by_hour_from_db(filters: Dict[str, Any]) -> Dict[str, int]:
    """Conversations started by hour-of-day (IST). Returns a dict
    hour ("0".."23") -> count, zero-filled for all 24 hours (mirrors
    ``get_calls_by_hour_from_db``)."""
    query, values = get_chats_by_hour_query(filters)
    try:
        rows = await run_parameterized_query(query, values)
        hours: Dict[str, int] = {str(h): 0 for h in range(24)}
        for row in rows or []:
            hour = row["hour"]
            if hour is not None and 0 <= hour <= 23:
                hours[str(hour)] = row["count"] or 0
        return hours
    except Exception as e:
        logger.error(f"Error fetching chats-by-hour: {e}", exc_info=True)
        raise
