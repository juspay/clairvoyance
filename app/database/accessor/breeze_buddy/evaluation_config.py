"""Database access for per-template evaluation configuration."""

import json
from typing import Any, Dict, List, Optional

import asyncpg

from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.evaluation_config import (
    add_discovered_topics_query,
    get_enabled_evaluations_query,
    get_evaluation_config_query,
    has_enabled_evaluations_query,
    initialize_evaluation_config_query,
    set_evaluation_enabled_query,
    update_evaluation_configuration_query,
    upsert_evaluation_configuration_query,
)


def _decode_evaluation_config_row(row: asyncpg.Record) -> Dict[str, Any]:
    """Decode JSONB fields returned as text by the shared asyncpg connection."""
    decoded = dict(row)
    configuration = decoded.get("configuration")
    if isinstance(configuration, str):
        configuration = json.loads(configuration)
    if configuration is not None and not isinstance(configuration, dict):
        raise ValueError("evaluation_config.configuration must be a JSON object")
    decoded["configuration"] = configuration or {}
    return decoded


async def initialize_evaluation_config(template_id: str) -> None:
    query, values = initialize_evaluation_config_query(template_id)
    await run_parameterized_query(query, values)


async def get_evaluation_config(
    template_id: str,
    evaluation_type: str = "TOPIC",
) -> Optional[Dict[str, Any]]:
    query, values = get_evaluation_config_query(template_id, evaluation_type)
    rows = await run_parameterized_query(query, values)
    return _decode_evaluation_config_row(rows[0]) if rows else None


async def get_enabled_evaluations(template_id: str) -> List[Dict[str, Any]]:
    query, values = get_enabled_evaluations_query(template_id)
    rows = await run_parameterized_query(query, values)
    return [_decode_evaluation_config_row(row) for row in rows or []]


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
    return _decode_evaluation_config_row(rows[0]) if rows else None


async def update_evaluation_configuration(
    template_id: str,
    evaluation_type: str,
    patch: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    query, values = update_evaluation_configuration_query(
        template_id, evaluation_type, patch
    )
    rows = await run_parameterized_query(query, values)
    return _decode_evaluation_config_row(rows[0]) if rows else None


async def upsert_evaluation_configuration(
    template_id: str,
    evaluation_type: str,
    enabled: bool,
    configuration: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    query, values = upsert_evaluation_configuration_query(
        template_id,
        evaluation_type,
        enabled,
        configuration,
    )
    rows = await run_parameterized_query(query, values)
    return _decode_evaluation_config_row(rows[0]) if rows else None


async def add_discovered_topics(template_id: str, labels: List[str]) -> None:
    query, values = add_discovered_topics_query(template_id, labels)
    await run_parameterized_query(query, values)
