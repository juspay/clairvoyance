"""
SQL queries for template operations.
"""

from typing import Any, Dict, List, Tuple

# Table name constants
TEMPLATE_TABLE = "template"


def get_template_by_merchant_query(
    merchant_id: str, shop_identifier: str = None, name: str = None
) -> Tuple[str, List[Any]]:
    """Generate query to get a template by merchant ID and optional filters."""
    conditions = ["merchant_id = $1"]
    values = [merchant_id]

    if shop_identifier:
        conditions.append(f"shop_identifier = ${len(values) + 1}")
        values.append(shop_identifier)

    if name:
        conditions.append(f"name = ${len(values) + 1}")
        values.append(name)

    query = f"""
        SELECT id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, is_active, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE {' AND '.join(conditions)}
        LIMIT 1
    """

    return query, values


def create_template_query(
    template_id: str,
    merchant_id: str,
    shop_identifier: str,
    name: str,
    flow: str,  # JSON string containing flow structure
    expected_payload_schema: str,  # JSON string containing expected payload schema
    expected_callback_response_schema: str,  # JSON string containing expected callback response schema
    configurations: str,  # JSON string containing configurations (tts_voice_name, stt_language, etc.)
    is_active: bool,
    created_at,
    updated_at,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new template."""
    query = f"""
        INSERT INTO {TEMPLATE_TABLE} (id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, is_active, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11)
        RETURNING id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, is_active, created_at, updated_at
    """

    return query, [
        template_id,
        merchant_id,
        shop_identifier,
        name,
        flow,
        expected_payload_schema,
        expected_callback_response_schema,
        configurations,
        is_active,
        created_at,
        updated_at,
    ]


def get_templates_list_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query to list multiple templates (metadata only, no flow).

    Supports RBAC filtering by merchant_ids and shop_identifiers arrays.

    Args:
        filters: Dictionary containing:
            - merchant_ids (optional): List of merchant IDs to filter by
            - shop_identifiers (optional): List of shop identifiers to filter by
            - is_active (optional): Filter by active status
            - merchant_id (optional): Single merchant ID to filter by
            - shop_identifier (optional): Single shop identifier to filter by

    Returns:
        Tuple of (query string, values list)
    """
    conditions = []
    values = []

    # Handle merchant filtering (supports both single and multiple)
    if "merchant_ids" in filters and filters["merchant_ids"]:
        values.append(filters["merchant_ids"])
        conditions.append(f"merchant_id = ANY(${len(values)})")
    elif "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f"merchant_id = ${len(values)}")

    # Handle shop filtering (supports both single and multiple)
    if "shop_identifiers" in filters and filters["shop_identifiers"]:
        values.append(filters["shop_identifiers"])
        conditions.append(f"shop_identifier = ANY(${len(values)})")
    elif "shop_identifier" in filters and filters["shop_identifier"]:
        values.append(filters["shop_identifier"])
        conditions.append(f"shop_identifier = ${len(values)}")

    # Handle is_active filter
    if "is_active" in filters:
        values.append(filters["is_active"])
        conditions.append(f"is_active = ${len(values)}")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Select only metadata columns (exclude flow and schema fields for performance)
    query = f"""
        SELECT id, merchant_id, shop_identifier, name, is_active, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        {where_clause}
        ORDER BY created_at DESC
    """

    return query, values


def get_template_by_id_query(template_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to get a single template by ID (includes full flow).

    Args:
        template_id: Template UUID

    Returns:
        Tuple of (query string, values list)
    """
    query = f"""
        SELECT id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, is_active, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE id = $1
        LIMIT 1
    """

    return query, [template_id]
