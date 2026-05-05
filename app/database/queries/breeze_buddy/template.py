"""
SQL queries for template operations.
"""

from typing import Any, Dict, List, Optional, Tuple

# Table name constants
TEMPLATE_TABLE = "template"


def get_template_by_merchant_query(
    reseller_id: str,
    merchant_id: Optional[str] = None,
    name: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Generate query to get a template by reseller ID and optional filters."""
    conditions = ["reseller_id = $1"]
    values = [reseller_id]

    if merchant_id:
        conditions.append(f"merchant_id = ${len(values) + 1}")
        values.append(merchant_id)
    else:
        conditions.append("merchant_id IS NULL")

    if name:
        conditions.append(f"name = ${len(values) + 1}")
        values.append(name)

    query = f"""
        SELECT id,
               reseller_id,
               merchant_id,
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE {" AND ".join(conditions)}
    """

    return query, values


def create_template_query(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    flow: str,  # JSON string containing flow structure
    expected_payload_schema: Optional[
        str
    ],  # JSON string containing expected payload schema
    expected_callback_response_schema: Optional[
        str
    ],  # JSON string containing expected callback response schema
    configurations: Optional[
        str
    ],  # JSON string containing configurations (tts_voice_name, stt_language, etc.)
    secrets: Optional[
        str
    ],  # JSON string containing secrets and variables for HTTP functions
    outbound_number_id: Optional[
        str
    ],  # Changed: moved before is_active to match SQL column order
    is_active: bool,
    supported_channels: List[str],
    created_at,
    updated_at,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new template."""
    query = f"""
        INSERT INTO {TEMPLATE_TABLE} (id, reseller_id, merchant_id, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10, $11, $12, $13, $14)
        RETURNING id, reseller_id, merchant_id, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at
    """

    return query, [
        template_id,
        reseller_id,
        merchant_id,
        name,
        flow,
        expected_payload_schema,
        expected_callback_response_schema,
        configurations,
        secrets,
        outbound_number_id,
        is_active,
        supported_channels,
        created_at,
        updated_at,
    ]


def delete_template_if_not_referenced_query(template_id: str) -> tuple[str, list]:
    """
    Atomically delete a template only if it is not referenced by call_execution_config or active leads.
    Returns the deleted row if successful, otherwise returns nothing.
    """
    query = f"""
        WITH can_delete AS (
            SELECT id FROM {TEMPLATE_TABLE}
            WHERE id = $1
            AND NOT EXISTS (
                SELECT 1 FROM call_execution_config WHERE template_id = $1
            )
            AND NOT EXISTS (
                SELECT 1 FROM lead_call_tracker WHERE template_id = $1 AND status IN ('BACKLOG', 'RETRY', 'PROCESSING')
            )
        )
        DELETE FROM {TEMPLATE_TABLE}
        WHERE id IN (SELECT id FROM can_delete)
        RETURNING id, reseller_id, merchant_id, name, is_active, created_at, updated_at
    """
    return query, [template_id]


def get_templates_list_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """
    Generate query to list multiple templates (metadata only, no flow).

    Supports RBAC filtering by reseller_ids and merchant_ids arrays.

    Args:
        filters: Dictionary containing:
            - reseller_ids (optional): List of reseller IDs to filter by
            - merchant_ids (optional): List of merchant IDs to filter by
            - is_active (optional): Filter by active status
            - reseller_id (optional): Single reseller ID to filter by
            - merchant_id (optional): Single merchant ID to filter by

    Returns:
        Tuple of (query string, values list)
    """
    conditions = []
    values = []

    # Handle reseller filtering (supports both single and multiple)
    if "reseller_ids" in filters and filters["reseller_ids"]:
        values.append(filters["reseller_ids"])
        conditions.append(f"reseller_id = ANY(${len(values)})")
    elif "reseller_id" in filters and filters["reseller_id"]:
        values.append(filters["reseller_id"])
        conditions.append(f"reseller_id = ${len(values)}")

    # Handle merchant filtering (supports both single and multiple)
    if "merchant_ids" in filters and filters["merchant_ids"]:
        values.append(filters["merchant_ids"])
        conditions.append(f"merchant_id = ANY(${len(values)})")
    elif "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f"merchant_id = ${len(values)}")

    # Handle is_active filter
    if "is_active" in filters:
        values.append(filters["is_active"])
        conditions.append(f"is_active = ${len(values)}")

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Select only metadata columns (exclude flow and schema fields for performance)
    query = f"""
        SELECT id, 
               reseller_id, 
               merchant_id, 
               name, is_active, created_at, updated_at
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
        SELECT id,
               reseller_id,
               merchant_id,
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE id = $1
        LIMIT 1
    """

    return query, [template_id]


def get_template_by_outbound_number_id_query(
    outbound_number_id: str,
    enable_inbound_only: bool = False,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get a template by outbound_number_id.

    Args:
        outbound_number_id: Outbound number UUID
        enable_inbound_only: If True, only return templates with
                             configurations.enable_inbound = true
    """
    conditions = ["outbound_number_id = $1"]

    if enable_inbound_only:
        # Filter by enable_inbound in configurations JSON
        # COALESCE ensures missing key defaults to FALSE
        conditions.append(
            "COALESCE((configurations->>'enable_inbound')::boolean, FALSE) = TRUE"
        )

    query = f"""
        SELECT id,
               reseller_id,
               merchant_id,
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE {' AND '.join(conditions)}
        LIMIT 1
    """
    return query, [outbound_number_id]


def get_all_templates_by_outbound_number_id_query(
    outbound_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get ALL templates by outbound_number_id.
    Used for IVR to list all available templates for a phone number.

    Only returns templates that are:
    - Active (is_active = TRUE)
    - Enabled for inbound (configurations.enable_inbound = true)

    Note: We intentionally do NOT select the `secrets` column here.
    This query is used only to list templates for IVR selection, and
    loading sensitive secrets is unnecessary in this context. The
    decoder will see `secrets` as None for these results, which is
    expected and by design.
    """
    query = f"""
        SELECT id,
               reseller_id,
               merchant_id,
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, outbound_number_id, is_active, supported_channels, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE outbound_number_id = $1
        AND is_active = TRUE
        AND COALESCE((configurations->>'enable_inbound')::boolean, FALSE) = TRUE
        ORDER BY 
            COALESCE((configurations->>'ivr_priority')::integer, 999999) ASC,
            name ASC
    """
    return query, [outbound_number_id]


def check_template_usage_query(template_id: str) -> Tuple[str, List[Any]]:
    """
    Generate query to check if a template is referenced by call_execution_config
    or has active leads in lead_call_tracker.

    Returns rows indicating where the template is in use:
    - source: 'call_execution_config' or 'lead_call_tracker'
    - reference_count: number of references found

    Args:
        template_id: Template UUID

    Returns:
        Tuple of (query string, values list)
    """
    query = """
        SELECT 'call_execution_config' AS source, COUNT(*) AS reference_count
        FROM call_execution_config
        WHERE template_id = $1

        UNION ALL

        SELECT 'lead_call_tracker' AS source, COUNT(*) AS reference_count
        FROM lead_call_tracker
        WHERE template_id = $1
        AND status IN ('BACKLOG', 'RETRY', 'PROCESSING')
    """

    return query, [template_id]


def replace_template_query(
    template_id: str,
    name: str,
    flow: str,
    expected_payload_schema: Optional[str],
    expected_callback_response_schema: Optional[str],
    configurations: Optional[str],
    secrets: Optional[str],
    outbound_number_id: Optional[str],
    is_active: bool,
    merchant_id: Optional[str],
    supported_channels: List[str],
    updated_at,
) -> Tuple[str, List[Any]]:
    """
    Generate query to replace a template.

    Args:
        template_id: Template UUID
        name: Template name (required)
        flow: Flow JSON string (required)
        expected_payload_schema: Expected payload schema JSON string or None
        expected_callback_response_schema: Expected callback response schema JSON string or None
        configurations: Configurations JSON string or None
        secrets: Secrets and variables JSON string or None
        outbound_number_id: Outbound number ID or None
        is_active: Whether template is active
        merchant_id: Merchant identifier or None
        supported_channels: Channels (voice/chat) the template is allowed on
        updated_at: Updated timestamp

    Returns:
        Tuple of (query string, values list)
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET name = $1,
            flow = $2::jsonb,
            expected_payload_schema = $3::jsonb,
            expected_callback_response_schema = $4::jsonb,
            configurations = $5::jsonb,
            secrets = $6::jsonb,
            outbound_number_id = $7,
            is_active = $8,
            merchant_id = $9,
            supported_channels = $10,
            updated_at = $11
        WHERE id = $12
        RETURNING id,
                  reseller_id,
                  merchant_id,
                  name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, outbound_number_id, is_active, supported_channels, created_at, updated_at
    """

    return query, [
        name,
        flow,
        expected_payload_schema,
        expected_callback_response_schema,
        configurations,
        secrets,
        outbound_number_id,
        is_active,
        merchant_id,
        supported_channels,
        updated_at,
        template_id,
    ]
