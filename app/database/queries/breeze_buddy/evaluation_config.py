"""SQL for per-template topic evaluation configuration."""

import json
from typing import Any, Dict, List, Tuple

_CONFIG_COLUMNS = "template_id, enabled, topics, configuration"


def get_evaluation_config_query(template_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT
            template.id AS template_id,
            COALESCE(
                ec.enabled,
                template.configurations -> 'enable_topic_evaluation'
                    = 'true'::jsonb,
                false
            ) AS enabled,
            COALESCE(ec.topics, ARRAY[]::text[]) AS topics,
            COALESCE(ec.configuration, defaults.configuration) AS configuration
        FROM template
        CROSS JOIN evaluation_config defaults
        LEFT JOIN evaluation_config ec ON ec.template_id = template.id
        WHERE template.id = $1::uuid
          AND defaults.template_id IS NULL
    """
    return query, [template_id]


def set_evaluation_enabled_query(
    template_id: str,
    enabled: bool,
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO evaluation_config (template_id, enabled, configuration)
        SELECT template.id, $2::boolean, defaults.configuration
        FROM template
        CROSS JOIN evaluation_config defaults
        WHERE template.id = $1::uuid
          AND template.configurations
                -> 'enable_topic_evaluation' = 'true'::jsonb
          AND defaults.template_id IS NULL
        ON CONFLICT (template_id) DO UPDATE
        SET enabled = EXCLUDED.enabled
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, enabled]


def update_evaluation_configuration_query(
    template_id: str,
    patch: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO evaluation_config (template_id, enabled, configuration)
        SELECT template.id, true, defaults.configuration || $2::jsonb
        FROM template
        CROSS JOIN evaluation_config defaults
        WHERE template.id = $1::uuid
          AND template.configurations
                -> 'enable_topic_evaluation' = 'true'::jsonb
          AND defaults.template_id IS NULL
        ON CONFLICT (template_id) DO UPDATE
        SET configuration = evaluation_config.configuration || $2::jsonb
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, json.dumps(patch)]
