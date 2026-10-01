"""Database access for topic evaluation configuration."""

from typing import Any, Dict, List, Optional

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_config import (
    add_discovered_topics_query,
    get_enabled_evaluations_query,
    get_evaluation_config_query,
    has_enabled_evaluations_query,
    initialize_evaluation_config_query,
    remove_topics_query,
    save_evaluation_configuration_query,
    set_evaluation_enabled_query,
    update_evaluation_configuration_query,
)


async def initialize_evaluation_config(template_id: str) -> None:
    query, values = initialize_evaluation_config_query(template_id)
    await run_parameterized_query(query, values)


async def get_evaluation_config(
    template_id: str,
    evaluation_type: str,
) -> Optional[Dict[str, Any]]:
    query, values = get_evaluation_config_query(template_id, evaluation_type)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def get_enabled_evaluations(template_id: str) -> List[Dict[str, Any]]:
    query, values = get_enabled_evaluations_query(template_id)
    rows = await run_parameterized_query(query, values)
    return [dict(row) for row in rows or []]


async def has_enabled_evaluations(template_id: str) -> bool:
    query, values = has_enabled_evaluations_query(template_id)
    rows = await run_parameterized_query(query, values)
    return bool(rows and rows[0]["enabled"])


async def set_evaluation_enabled(
    template_id: str,
    evaluation_type: str,
    enabled: bool,
) -> Optional[Dict[str, Any]]:
    query, values = set_evaluation_enabled_query(template_id, evaluation_type, enabled)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def update_evaluation_configuration(
    template_id: str,
    evaluation_type: str,
    patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    query, values = update_evaluation_configuration_query(
        template_id, evaluation_type, patch
    )
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def save_evaluation_configuration(
    template_id: str,
    evaluation_type: str,
    configuration: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    query, values = save_evaluation_configuration_query(
        template_id, evaluation_type, configuration
    )
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def add_discovered_topics(
    template_id: str, labels: List[str]
) -> Optional[Dict[str, Any]]:
    query, values = add_discovered_topics_query(template_id, labels)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None


async def remove_topics(
    template_id: str, labels: List[str]
) -> Optional[Dict[str, Any]]:
    query, values = remove_topics_query(template_id, labels)
    rows = await run_parameterized_query(query, values)
    return dict(rows[0]) if rows else None
