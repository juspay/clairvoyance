"""
Database accessor functions for alert_groups table.
"""

from typing import Dict, List, Optional

from app.core.logger import logger
from app.database.decoder.breeze_buddy.alert_groups import decode_alert_group
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.alert_groups import (
    get_alert_group_by_name_query,
    upsert_alert_group_query,
)
from app.schemas.breeze_buddy.alerts import AlertGroup


async def get_alert_group_by_name(name: str, reseller_id: str) -> Optional[AlertGroup]:
    """
    Fetch alert group by name, scoped to a reseller.

    Returns:
        AlertGroup model, or None if not found.
    """
    try:
        text, values = get_alert_group_by_name_query(name, reseller_id)
        result = await run_parameterized_query(text, values)
        return decode_alert_group(result[0] if result else None)
    except Exception as e:
        logger.error(
            f"Error fetching alert group '{name}' for reseller '{reseller_id}': {e}"
        )
        raise


async def upsert_alert_group(
    name: str, reseller_id: str, members: List[Dict[str, str]]
) -> Optional[AlertGroup]:
    """
    Create or update an alert group scoped to a reseller.

    Args:
        name: Group name (unique per reseller)
        reseller_id: Owning reseller identifier
        members: List of dicts with 'name' and 'phone' keys

    Returns:
        The created/updated AlertGroup model, or None on failure.
    """
    try:
        text, values = upsert_alert_group_query(name, reseller_id, members)
        result = await run_parameterized_query(text, values)
        return decode_alert_group(result[0] if result else None)
    except Exception as e:
        logger.error(
            f"Error upserting alert group '{name}' for reseller '{reseller_id}': {e}"
        )
        raise
