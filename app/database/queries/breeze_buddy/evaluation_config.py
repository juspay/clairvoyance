"""SQL for per-template topic evaluation configuration."""

import json
from typing import Any, Dict, List, Tuple

_CONFIG_COLUMNS = (
    "id, template_id, evaluation_type::text AS evaluation_type, "
    "enabled, topics, configuration"
)


def get_evaluation_config_query(
    template_id: str,
    evaluation_type: str,
) -> Tuple[str, List[Any]]:
    """The agent's own row for this type (no defaults fallback)."""
    query = f"""
        SELECT {_CONFIG_COLUMNS}
        FROM evaluation_config
        WHERE template_id = $1::uuid
          AND evaluation_type = $2::evaluation_type
    """
    return query, [template_id, evaluation_type]


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
        SELECT id, evaluation_type::text AS evaluation_type, topics, configuration,
               configuration ->> 'model' AS model
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
    evaluation_type: str,
    enabled: bool,
) -> Tuple[str, List[Any]]:
    """Flip the agent's existing row — never creates one.

    No row (or a disabled row) means the agent does not run this
    evaluation."""
    query = f"""
        UPDATE evaluation_config
        SET enabled = $3::boolean
        WHERE template_id = $1::uuid
          AND evaluation_type = $2::evaluation_type
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, evaluation_type, enabled]


def update_evaluation_configuration_query(
    template_id: str,
    evaluation_type: str,
    patch: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Shallow JSONB merge on the agent's existing row — never creates one.

    Each top-level key in the patch overwrites the stored one wholesale.
    TOPIC sends partial patches; CONVERSATION_EVALS's validator requires
    every key, so for it the merge amounts to a full replacement."""
    query = f"""
        UPDATE evaluation_config
        SET configuration = configuration || $3::jsonb
        WHERE template_id = $1::uuid
          AND evaluation_type = $2::evaluation_type
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, evaluation_type, json.dumps(patch)]


def save_evaluation_configuration_query(
    template_id: str,
    evaluation_type: str,
    configuration: Dict[str, Any],
) -> Tuple[str, List[Any]]:
    """Create-or-replace — the row's creation point (POST).

    A missing row is born DISABLED: configuring an evaluation is not
    consenting to run it, enable is a separate flip (and it never
    creates). An existing row gets its configuration replaced wholesale
    and keeps its enabled flag."""
    query = f"""
        INSERT INTO evaluation_config (
            template_id, evaluation_type, enabled, configuration
        )
        VALUES ($1::uuid, $2::evaluation_type, false, $3::jsonb)
        ON CONFLICT (template_id, evaluation_type)
            DO UPDATE SET configuration = EXCLUDED.configuration
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, evaluation_type, json.dumps(configuration)]


def add_discovered_topics_query(
    template_id: str,
    labels: List[str],
) -> Tuple[str, List[Any]]:
    query = f"""
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
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, labels]


def remove_topics_query(
    template_id: str,
    labels: List[str],
) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE evaluation_config config
        SET topics = ARRAY(
            SELECT existing.label
            FROM unnest(config.topics) WITH ORDINALITY AS existing(label, position)
            WHERE lower(btrim(existing.label)) <> ALL(
                SELECT lower(btrim(removed.label))
                FROM unnest($2::text[]) AS removed(label)
            )
            ORDER BY existing.position
        )
        WHERE config.template_id = $1::uuid
          AND config.evaluation_type = 'TOPIC'
        RETURNING {_CONFIG_COLUMNS}
    """
    return query, [template_id, labels]
