"""
SQL queries for template operations.
"""

from typing import Any, List, Tuple

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
        SELECT id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, is_active, created_at, updated_at
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
    is_active: bool,
    created_at,
    updated_at,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new template."""
    query = f"""
        INSERT INTO {TEMPLATE_TABLE} (id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, is_active, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9, $10)
        RETURNING id, merchant_id, shop_identifier, name, flow, expected_payload_schema, expected_callback_response_schema, is_active, created_at, updated_at
    """

    return query, [
        template_id,
        merchant_id,
        shop_identifier,
        name,
        flow,
        expected_payload_schema,
        expected_callback_response_schema,
        is_active,
        created_at,
        updated_at,
    ]
