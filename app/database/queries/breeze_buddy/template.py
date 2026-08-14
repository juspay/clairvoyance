"""
SQL queries for template operations.
"""

from typing import Any, Dict, List, Optional, Tuple

# Table name constants
TEMPLATE_TABLE = "template"


def get_template_in_scope_query(
    reseller_id: str,
    merchant_id: Optional[str] = None,
    name: Optional[str] = None,
) -> Tuple[str, List[Any]]:
    """Query a template by its exact (reseller, merchant, name) scope.

    merchant_id=None matches reseller-level rows only (merchant_id IS NULL);
    no fallback. Runtime resolution is id-only — this serves uniqueness
    checks and fixed internal lookups.
    """
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
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
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
    telephony_number_id: Optional[
        str
    ],  # Changed: moved before is_active to match SQL column order
    is_active: bool,
    supported_channels: List[str],
    created_at,
    updated_at,
) -> Tuple[str, List[Any]]:
    """Generate query to create a new template."""
    query = f"""
        INSERT INTO {TEMPLATE_TABLE} (id, reseller_id, merchant_id, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, created_at, updated_at)
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9::jsonb, $10, $11, $12, $13, $14)
        RETURNING id, reseller_id, merchant_id, name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
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
        telephony_number_id,
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
        RETURNING id, reseller_id, merchant_id, name, is_active, family_id,
                  current_version, created_at, updated_at
    """
    return query, [template_id]


def _build_template_list_conditions(
    filters: Dict[str, Any],
) -> Tuple[List[str], List[Any]]:
    """Shared WHERE builder for the templates list + count queries.

    Keeps both in sync. Supports single/array reseller and merchant filters,
    is_active, and a case-insensitive name search.
    """
    conditions: List[str] = []
    values: List[Any] = []

    if "reseller_ids" in filters and filters["reseller_ids"]:
        values.append(filters["reseller_ids"])
        conditions.append(f"reseller_id = ANY(${len(values)})")
    elif "reseller_id" in filters and filters["reseller_id"]:
        values.append(filters["reseller_id"])
        conditions.append(f"reseller_id = ${len(values)}")

    if "merchant_ids" in filters and filters["merchant_ids"]:
        values.append(filters["merchant_ids"])
        conditions.append(f"merchant_id = ANY(${len(values)})")
    elif "merchant_id" in filters and filters["merchant_id"]:
        values.append(filters["merchant_id"])
        conditions.append(f"merchant_id = ${len(values)}")
    elif filters.get("merchant_id_is_null"):
        # Generic (reseller-level) templates only — used by the list
        # fallback so it cannot widen beyond merchant_id IS NULL rows.
        conditions.append("merchant_id IS NULL")

    if "is_active" in filters:
        values.append(filters["is_active"])
        conditions.append(f"is_active = ${len(values)}")

    if filters.get("family_id_is_null"):
        # Unassigned agents only — used by the family pickers so admins
        # cannot accidentally add an agent that already belongs to another
        # family.
        conditions.append("family_id IS NULL")

    if filters.get("search"):
        values.append(f"%{filters['search']}%")
        conditions.append(f"name ILIKE ${len(values)}")

    return conditions, values


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
    conditions, values = _build_template_list_conditions(filters)

    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""

    # Paginated requests sort by name so the query can ride the
    # (reseller_id, merchant_id, name) composite index. Unpaginated requests
    # keep the original newest-first ordering for backward compatibility.
    paginate = filters.get("limit") is not None
    order_by = "name" if paginate else "created_at DESC"

    # Select only metadata columns (exclude flow and schema fields for performance)
    query = f"""
        SELECT id,
               reseller_id,
               merchant_id,
               name, is_active, supported_channels, family_id, current_version, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        {where_clause}
        ORDER BY {order_by}
    """

    if paginate:
        values.append(filters["limit"])
        query += f"\n        LIMIT ${len(values)}"
        values.append(filters.get("offset", 0))
        query += f"\n        OFFSET ${len(values)}"

    return query, values


def get_templates_count_query(filters: Dict[str, Any]) -> Tuple[str, List[Any]]:
    """Generate a COUNT(*) query matching get_templates_list_query's filters.

    Used for pagination totals. Ignores limit/offset/ordering.
    """
    conditions, values = _build_template_list_conditions(filters)
    where_clause = " WHERE " + " AND ".join(conditions) if conditions else ""
    return f"SELECT COUNT(*) AS total FROM {TEMPLATE_TABLE}{where_clause}", values


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
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE id = $1
        LIMIT 1
    """

    return query, [template_id]


def get_merchant_by_template_id_query(template_id: str) -> Tuple[str, List[Any]]:
    """Generate query to read only a template's owning merchant."""
    query = f"""
        SELECT merchant_id
        FROM {TEMPLATE_TABLE}
        WHERE id = $1
        LIMIT 1
    """

    return query, [template_id]


def get_template_by_telephony_number_id_query(
    telephony_number_id: str,
    enable_inbound_only: bool = False,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get a template by telephony_number_id.

    Args:
        telephony_number_id: Telephony number UUID
        enable_inbound_only: If True, only return templates with
                             configurations.enable_inbound = true
    """
    conditions = ["telephony_number_id = $1"]

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
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE {' AND '.join(conditions)}
        LIMIT 1
    """
    return query, [telephony_number_id]


def get_all_templates_by_telephony_number_id_query(
    telephony_number_id: str,
) -> Tuple[str, List[Any]]:
    """
    Generate query to get ALL templates by telephony_number_id.
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
               name, flow, expected_payload_schema, expected_callback_response_schema, configurations, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE telephony_number_id = $1
        AND is_active = TRUE
        AND COALESCE((configurations->>'enable_inbound')::boolean, FALSE) = TRUE
        ORDER BY 
            COALESCE((configurations->>'ivr_priority')::integer, 999999) ASC,
            name ASC
    """
    return query, [telephony_number_id]


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
    reseller_id: str,
    name: str,
    flow: str,
    expected_payload_schema: Optional[str],
    expected_callback_response_schema: Optional[str],
    configurations: Optional[str],
    secrets: Optional[str],
    telephony_number_id: Optional[str],
    is_active: bool,
    merchant_id: Optional[str],
    supported_channels: List[str],
    updated_at,
) -> Tuple[str, List[Any]]:
    """
    Generate query to replace a template.

    Args:
        template_id: Template UUID
        reseller_id: Reseller identifier (required)
        name: Template name (required)
        flow: Flow JSON string (required)
        expected_payload_schema: Expected payload schema JSON string or None
        expected_callback_response_schema: Expected callback response schema JSON string or None
        configurations: Configurations JSON string or None
        secrets: Secrets and variables JSON string or None
        telephony_number_id: Telephony number ID or None
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
            telephony_number_id = $7,
            is_active = $8,
            reseller_id = $9,
            merchant_id = $10,
            supported_channels = $11,
            current_version = current_version + 1,
            updated_at = $12
        WHERE id = $13
        RETURNING id,
                  reseller_id,
                  merchant_id,
                  name, flow, expected_payload_schema, expected_callback_response_schema, configurations, secrets, telephony_number_id, is_active, supported_channels, family_id, current_version, created_at, updated_at
    """

    return query, [
        name,
        flow,
        expected_payload_schema,
        expected_callback_response_schema,
        configurations,
        secrets,
        telephony_number_id,
        is_active,
        reseller_id,
        merchant_id,
        supported_channels,
        updated_at,
        template_id,
    ]


def get_templates_for_update_query(template_ids: List[str]) -> Tuple[str, List[Any]]:
    """Lock + load full head rows for a bulk operation (row locks prevent a
    concurrent PUT from interleaving between read and write)."""
    query = f"""
        SELECT id, reseller_id, merchant_id, name, flow,
               expected_payload_schema, expected_callback_response_schema,
               configurations, secrets, telephony_number_id, is_active,
               supported_channels, family_id, current_version,
               created_at, updated_at
        FROM {TEMPLATE_TABLE}
        WHERE id = ANY($1::uuid[])
        ORDER BY id
        FOR UPDATE
    """
    return query, [template_ids]


def update_template_bulk_fields_query(
    template_id: str, flow_json: str, configurations_json: Optional[str], now
) -> Tuple[str, List[Any]]:
    """Bulk update touches only flow + configurations (patched values);
    identity, secrets, pins, channels are never bulk-edited."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET flow = $1::jsonb,
            configurations = $2::jsonb,
            current_version = current_version + 1,
            updated_at = $3
        WHERE id = $4
        RETURNING id, reseller_id, merchant_id, name, flow,
                  expected_payload_schema, expected_callback_response_schema,
                  configurations, secrets, telephony_number_id, is_active,
                  supported_channels, family_id, current_version,
                  created_at, updated_at
    """
    return query, [flow_json, configurations_json, now, template_id]


_FAMILY_MEMBER_COLUMNS = (
    "id, reseller_id, merchant_id, name, flow, "
    "expected_payload_schema, expected_callback_response_schema, "
    "configurations, secrets, telephony_number_id, is_active, "
    "supported_channels, family_id, current_version, "
    "derived_from_base_version, created_at, updated_at"
)


def get_family_templates_for_update_query(family_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_FAMILY_MEMBER_COLUMNS}
        FROM {TEMPLATE_TABLE}
        WHERE family_id = $1
        ORDER BY id
        FOR UPDATE
    """
    return query, [family_id]


def get_family_member_contents_query(
    family_id: str,
    limit: Optional[int] = None,
    offset: int = 0,
) -> Tuple[str, List[Any]]:
    """Read-only twin of ``get_family_templates_for_update_query`` — same
    columns, no ``FOR UPDATE``. Propagation preview must not take row locks:
    it writes nothing and must never block a concurrent PUT.

    ``limit`` / ``offset`` enable paginated preview: pass them together to
    fetch one page of members; omit both to fetch all (legacy / apply path).
    """
    values: List[Any] = [family_id]
    query = f"""
        SELECT {_FAMILY_MEMBER_COLUMNS}
        FROM {TEMPLATE_TABLE}
        WHERE family_id = $1
        ORDER BY id
    """
    if limit is not None:
        values.append(limit)
        query += f"\n        LIMIT ${len(values)}"
        values.append(offset)
        query += f"\n        OFFSET ${len(values)}"
    return query, values


def count_family_member_contents_query(family_id: str) -> Tuple[str, List[Any]]:
    """Total number of template members in a family — for pagination metadata."""
    query = f"""
        SELECT COUNT(*) AS total
        FROM {TEMPLATE_TABLE}
        WHERE family_id = $1
    """
    return query, [family_id]


def set_template_derived_base_version_query(
    template_id: str, base_version: Optional[int]
) -> Tuple[str, List[Any]]:
    """Record which family revision a child is now synced to.

    Lineage metadata, not content: deliberately does NOT bump
    current_version and is not part of the version snapshot — the template's
    content is unchanged by this statement.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET derived_from_base_version = $1
        WHERE id = $2
    """
    return query, [base_version, template_id]


def set_templates_derived_base_version_query(
    template_ids: List[str], base_version: Optional[int]
) -> Tuple[str, List[Any]]:
    """Bulk form, used when a propagation is reverted."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET derived_from_base_version = $1
        WHERE id = ANY($2::uuid[])
    """
    return query, [base_version, template_ids]


def assign_family_query(
    family_id: str, template_ids: List[str], now
) -> Tuple[str, List[Any]]:
    """Adopt templates into a family.

    Families are global admin-managed groups: any existing template can be
    assigned regardless of reseller, provided it is not already a member of
    a *different* family.  Templates already in *this* family are a no-op.
    Templates belonging to another family are excluded by the WHERE clause
    and therefore absent from RETURNING id, which causes _assert_all_assigned
    to raise a 409 with a clear message.
    """
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET family_id = $1,
            updated_at = $2
        WHERE id = ANY($3::uuid[])
          AND (family_id IS NULL OR family_id = $1::uuid)
        RETURNING id
    """
    return query, [family_id, now, template_ids]


def get_template_head_versions_query(template_ids: List[str]) -> Tuple[str, List[Any]]:
    """Lock + read just the head version numbers for a bulk rollback's drift
    check (row locks prevent a concurrent edit racing the rollback)."""
    query = f"""
        SELECT id, current_version
        FROM {TEMPLATE_TABLE}
        WHERE id = ANY($1::uuid[])
        FOR UPDATE
    """
    return query, [template_ids]


def restore_template_head_query(
    template_id: str,
    reseller_id: str,
    merchant_id: Optional[str],
    name: str,
    flow_json: str,
    expected_payload_schema_json: Optional[str],
    expected_callback_response_schema_json: Optional[str],
    configurations_json: Optional[str],
    secrets_json: Optional[str],
    telephony_number_id: Optional[str],
    is_active: bool,
    supported_channels: List[str],
    now,
) -> Tuple[str, List[Any]]:
    """Rollback write: restore all editable columns from a snapshot and bump
    current_version (rollback is itself a new version, never history surgery)."""
    query = f"""
        UPDATE {TEMPLATE_TABLE}
        SET reseller_id = $1,
            merchant_id = $2,
            name = $3,
            flow = $4::jsonb,
            expected_payload_schema = $5::jsonb,
            expected_callback_response_schema = $6::jsonb,
            configurations = $7::jsonb,
            secrets = $8::jsonb,
            telephony_number_id = $9,
            is_active = $10,
            supported_channels = $11,
            current_version = current_version + 1,
            updated_at = $12
        WHERE id = $13
        RETURNING id, reseller_id, merchant_id, name, flow,
                  expected_payload_schema, expected_callback_response_schema,
                  configurations, secrets, telephony_number_id, is_active,
                  supported_channels, family_id, current_version,
                  created_at, updated_at
    """
    return query, [
        reseller_id,
        merchant_id,
        name,
        flow_json,
        expected_payload_schema_json,
        expected_callback_response_schema_json,
        configurations_json,
        secrets_json,
        telephony_number_id,
        is_active,
        supported_channels,
        now,
        template_id,
    ]
