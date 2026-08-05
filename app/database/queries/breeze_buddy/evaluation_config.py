"""SQL for per-template topic evaluation configuration."""

import json
from typing import Any, Dict, List, Tuple

_CONFIG_COLUMNS = (
    "id, template_id, evaluation_type::text AS evaluation_type, "
    "enabled, topics, configuration"
)


def get_evaluation_config_query(template_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_CONFIG_COLUMNS}
        FROM evaluation_config
        WHERE template_id = $1::uuid
          AND evaluation_type = 'TOPIC'
    """
    return query, [template_id]


def initialize_evaluation_config_query(template_id: str) -> Tuple[str, List[Any]]:
    query = """
        INSERT INTO evaluation_config (
            template_id, evaluation_type, enabled, configuration
        )
        SELECT
            template.id, defaults.evaluation_type, true, defaults.configuration
        FROM template
        CROSS JOIN evaluation_config defaults
        WHERE template.id = $1::uuid
          AND template.configurations
                -> 'enable_topic_evaluation' = 'true'::jsonb
          AND defaults.template_id IS NULL
          AND defaults.evaluation_type = 'TOPIC'
        ON CONFLICT (template_id, evaluation_type) DO NOTHING
    """
    return query, [template_id]


def get_enabled_evaluations_query(template_id: str) -> Tuple[str, List[Any]]:
    query = """
        SELECT id, evaluation_type::text AS evaluation_type, topics, configuration
        FROM evaluation_config
        WHERE template_id = $1::uuid
          AND enabled
    """
    return query, [template_id]


def has_enabled_evaluations_query(template_id: str) -> Tuple[str, List[Any]]:
    query = """
        SELECT EXISTS (
            SELECT 1
            FROM evaluation_config
            WHERE template_id = $1::uuid
              AND enabled
        ) AS enabled
    """
    return query, [template_id]


def set_evaluation_enabled_query(
    template_id: str,
    enabled: bool,
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE evaluation_config
        SET enabled = $2::boolean
        WHERE template_id = $1::uuid
          AND evaluation_type = 'TOPIC'
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, enabled]


def update_evaluation_configuration_query(
    template_id: str,
    patch: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE evaluation_config
        SET configuration = configuration || $2::jsonb
        WHERE template_id = $1::uuid
          AND evaluation_type = 'TOPIC'
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, json.dumps(patch)]


def add_discovered_topics_query(
    template_id: str,
    labels: List[str],
) -> Tuple[str, List[Any]]:
    query = """
        UPDATE evaluation_config config
        SET topics = config.topics || ARRAY(
            SELECT label
            FROM unnest($2::text[]) AS discovered(label)
            WHERE NOT EXISTS (
                SELECT 1
                FROM unnest(config.topics) AS existing(label)
                WHERE lower(btrim(existing.label)) = lower(btrim(discovered.label))
            )
        )
        WHERE config.template_id = $1::uuid
          AND config.evaluation_type = 'TOPIC'
    """
    return query, [template_id, labels]
