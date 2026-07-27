"""Database access for topic evaluation configuration."""

from typing import Any, Dict, Optional

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_config import (
    get_evaluation_config_query,
    set_evaluation_enabled_query,
    update_evaluation_configuration_query,
)


async def get_evaluation_config(template_id: str) -> Optional[Dict[str, Any]]:
    query, values = get_evaluation_config_query(template_id)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def set_evaluation_enabled(
    template_id: str,
    enabled: bool,
) -> Optional[Dict[str, Any]]:
    query, values = set_evaluation_enabled_query(template_id, enabled)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def update_evaluation_configuration(
    template_id: str,
    patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    query, values = update_evaluation_configuration_query(template_id, patch)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None
