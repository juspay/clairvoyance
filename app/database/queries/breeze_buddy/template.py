"""
SQL queries for template operations.
"""

from typing import Any, Dict, List, Optional, Tuple

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
    else:
        conditions.append("shop_identifier IS NULL")

    if name:
        conditions.append(f"name = ${len(values) + 1}")
        values.append(name)

    query = f"""
        SELECT id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, outbound_number_id, is_active, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE {' AND '.join(conditions)}
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
    outbound_number_id: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new template."""
    query = f"""
        INSERT INTO {TEMPLATE_TABLE} (id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, outbound_number_id, is_active, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11, $12)
        RETURNING id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, outbound_number_id, is_active, created_at, updated_at
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
        outbound_number_id,
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


def update_template_query(
    template_id: str, update_fields: Dict[str, Any], updated_at
) -> Tuple[str, List[Any]]:
    """
    Generate query to update a template with partial updates.

    Args:
        template_id: Template UUID
        update_fields: Dictionary of fields to update (field_name -> value)
        updated_at: Timestamp for updated_at field

    Returns:
        Tuple of (query string, values list)
    """
    if not update_fields:
        raise ValueError("No fields to update")

    set_clauses = []
    values = []

    # Map field names to database column names and handle JSON serialization
    field_mapping = {
        "template_name": "name",
        "identifier": "shop_identifier",
        "outbound_number_id": "outbound_number_id",
        "is_active": "is_active",
        "flow": "flow",
        "expected_payload_schema": "expected_payload_schema",
        "expected_callback_response_schema": "expected_callback_response_schema",
        "configurations": "configurations",
    }

    # Fields that need to be cast to jsonb
    jsonb_fields = {
        "flow",
        "expected_payload_schema",
        "expected_callback_response_schema",
        "configurations",
    }

    for field_name, value in update_fields.items():
        if field_name in field_mapping:
            db_column = field_mapping[field_name]
            values.append(value)

            if db_column in jsonb_fields:
                set_clauses.append(f"{db_column} = ${len(values)}::jsonb")
            else:
                set_clauses.append(f"{db_column} = ${len(values)}")

    # Always update the updated_at timestamp
    values.append(updated_at)
    set_clauses.append(f"updated_at = ${len(values)}")

    # Add template_id as last parameter for WHERE clause
    values.append(template_id)

    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET {', '.join(set_clauses)}
        WHERE id = ${len(values)}
        RETURNING id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, outbound_number_id, is_active, created_at, updated_at
    """

    return query, values
