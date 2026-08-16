"""
Database query functions for template version history.

Append-only snapshots of a template's versioned content. The active
version is always MAX(version_number). See docs/TEMPLATE_VERSIONING.md.
"""

from typing import Any, List, Optional, Tuple

TEMPLATE_VERSION_TABLE = "template_version"


def insert_template_version_query(
    template_id: str,
    name: str,
    flow: str,
    configurations: Optional[str],
    expected_payload_schema: Optional[str],
    expected_callback_response_schema: Optional[str],
    updated_by: Optional[str],
    change_source: str,
    restored_from: Optional[int],
) -> Tuple[str, List[Any]]:
    """Append the next version snapshot for a template.

    version_number is computed by a scalar subquery. This is race-free
    because every caller runs inside a transaction that has already
    row-locked the parent template row (the in-place UPDATE, or the
    just-inserted row on create); the UNIQUE (template_id, version_number)
    constraint is the backstop.
    """
    query = f"""
        INSERT INTO {TEMPLATE_VERSION_TABLE} (
            template_id, version_number, name, flow, configurations,
            expected_payload_schema, expected_callback_response_schema,
            updated_by, change_source, restored_from
        )
        VALUES (
            $1,
            (
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM {TEMPLATE_VERSION_TABLE}
                WHERE template_id = $1
            ),
            $2, $3::jsonb, $4::jsonb, $5::jsonb, $6::jsonb, $7, $8, $9
        )
        RETURNING version_number
    """
    return query, [
        template_id,
        name,
        flow,
        configurations,
        expected_payload_schema,
        expected_callback_response_schema,
        updated_by,
        change_source,
        restored_from,
    ]


def list_template_versions_query(
    template_id: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    """History-panel listing: metadata only, newest first, no JSON blobs."""
    query = f"""
        SELECT id, template_id, version_number, name, updated_by,
               change_source, restored_from, created_at
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1
        ORDER BY version_number DESC
        LIMIT $2 OFFSET $3
    """
    return query, [template_id, limit, offset]


def count_template_versions_query(template_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT COUNT(*) AS total
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1
    """
    return query, [template_id]


def get_template_version_query(
    template_id: str, version_number: int
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT id, template_id, version_number, name, flow, configurations,
               expected_payload_schema, expected_callback_response_schema,
               updated_by, change_source, restored_from, created_at
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1 AND version_number = $2
    """
    return query, [template_id, version_number]
