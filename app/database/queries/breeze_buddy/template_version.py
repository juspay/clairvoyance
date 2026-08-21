"""SQL queries for template versioning (lineage) and bulk operations."""

from typing import Any, List, Optional, Tuple

TEMPLATE_VERSION_TABLE = "template_version"
TEMPLATE_BULK_OP_TABLE = "template_bulk_op"


def insert_template_version_query(
    template_id: str,
    version: int,
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
    change_source: str,
    bulk_op_id: Optional[str],
    changed_by: Optional[str],
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {TEMPLATE_VERSION_TABLE} (
            template_id, version, reseller_id, merchant_id, name, flow,
            expected_payload_schema, expected_callback_response_schema,
            configurations, secrets, telephony_number_id, is_active,
            supported_channels, change_source, bulk_op_id, changed_by
        )
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb,
                $9::jsonb, $10::jsonb, $11, $12, $13, $14, $15, $16)
        RETURNING id, template_id, version, change_source, bulk_op_id,
                  changed_by, created_at
    """
    return query, [
        template_id,
        version,
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
        change_source,
        bulk_op_id,
        changed_by,
    ]


def get_template_versions_query(
    template_id: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT template_id, version, change_source, bulk_op_id,
               changed_by, created_at
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1
        ORDER BY version DESC
        LIMIT $2 OFFSET $3
    """
    return query, [template_id, limit, offset]


def get_template_versions_count_query(template_id: str) -> Tuple[str, List[Any]]:
    return (
        f"SELECT COUNT(*) AS total FROM {TEMPLATE_VERSION_TABLE} WHERE template_id = $1",
        [template_id],
    )


def get_template_version_snapshot_query(
    template_id: str, version: int
) -> Tuple[str, List[Any]]:
    # template_id AS id + created_at AS updated_at let decode_template
    # (decoder/breeze_buddy/template.py) decode a snapshot row unchanged.
    query = f"""
        SELECT template_id AS id,
               version, version AS current_version,
               reseller_id, merchant_id, name, flow,
               expected_payload_schema, expected_callback_response_schema,
               configurations, secrets, telephony_number_id, is_active,
               supported_channels, change_source, bulk_op_id, changed_by,
               created_at, created_at AS updated_at
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1 AND version = $2
        LIMIT 1
    """
    return query, [template_id, version]


def insert_bulk_op_query(
    op_id: str,
    op_type: str,
    family_id: Optional[str],
    template_ids: List[str],
    patch_json: Optional[str],
    status: str,
    reverted_bulk_op_id: Optional[str],
    initiated_by: Optional[str],
    from_base_version: Optional[int] = None,
    to_base_version: Optional[int] = None,
) -> Tuple[str, List[Any]]:
    """``from_base_version``/``to_base_version`` are set only by propagation
    ops (which family revision the children moved from / to); the two
    Phase-1 call sites leave them NULL."""
    query = f"""
        INSERT INTO {TEMPLATE_BULK_OP_TABLE} (
            id, op_type, family_id, template_ids, patch, status,
            reverted_bulk_op_id, initiated_by, from_base_version,
            to_base_version
        )
        VALUES ($1, $2, $3, $4::uuid[], $5::jsonb, $6, $7, $8, $9, $10)
        RETURNING id, op_type, family_id, template_ids, status,
                  reverted_bulk_op_id, initiated_by, from_base_version,
                  to_base_version, created_at
    """
    return query, [
        op_id,
        op_type,
        family_id,
        template_ids,
        patch_json,
        status,
        reverted_bulk_op_id,
        initiated_by,
        from_base_version,
        to_base_version,
    ]


def get_bulk_op_by_id_query(bulk_op_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT id, op_type, family_id, template_ids, patch, status,
               reverted_bulk_op_id, initiated_by, from_base_version,
               to_base_version, created_at
        FROM {TEMPLATE_BULK_OP_TABLE}
        WHERE id = $1
        LIMIT 1
    """
    return query, [bulk_op_id]


def list_bulk_ops_query(limit: int, offset: int) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT id, op_type, family_id, template_ids, patch, status,
               reverted_bulk_op_id, initiated_by, from_base_version,
               to_base_version, created_at
        FROM {TEMPLATE_BULK_OP_TABLE}
        ORDER BY created_at DESC
        LIMIT $1 OFFSET $2
    """
    return query, [limit, offset]


def mark_bulk_op_rolled_back_query(bulk_op_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        UPDATE {TEMPLATE_BULK_OP_TABLE}
        SET status = 'rolled_back'
        WHERE id = $1
        RETURNING id, status
    """
    return query, [bulk_op_id]


def get_bulk_op_touched_versions_query(bulk_op_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT template_id, version
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE bulk_op_id = $1
        ORDER BY template_id
    """
    return query, [bulk_op_id]


def prune_template_versions_query(
    template_id: str, current_version: int, keep: int
) -> Tuple[str, List[Any]]:
    """Retention: keep only the newest ``keep`` versions of a template.

    Rollback-safety guard: never deletes rows an active (completed, not yet
    rolled back) bulk op still needs — each member's bulk-written version
    AND its immediate predecessor (the pre-op snapshot bulk rollback
    restores), and nothing else. Runs in the same transaction as the
    snapshot insert.
    """
    query = f"""
        DELETE FROM {TEMPLATE_VERSION_TABLE} tv
        WHERE tv.template_id = $1
          AND tv.version <= $2::integer - $3::integer
          AND NOT EXISTS (
                SELECT 1
                FROM {TEMPLATE_VERSION_TABLE} bv
                JOIN {TEMPLATE_BULK_OP_TABLE} op ON op.id = bv.bulk_op_id
                WHERE bv.template_id = tv.template_id
                  AND op.status = 'completed'
                  AND tv.version >= bv.version - 1
                  AND tv.version <= bv.version
          )
    """
    return query, [template_id, current_version, keep]


def count_missing_preop_snapshots_bulk_query(
    bulk_op_ids: List[str],
) -> Tuple[str, List[Any]]:
    """How many templates touched by each op can no longer be rolled back
    (their pre-op snapshot was pruned by retention), for many ops in one round
    trip — ``list_bulk_ops`` would otherwise run one count per completed op in
    the page. Ops with zero missing snapshots are simply absent from the result
    set; the caller defaults them to 0.
    """
    query = f"""
        SELECT bv.bulk_op_id AS bulk_op_id, COUNT(*) AS missing
        FROM {TEMPLATE_VERSION_TABLE} bv
        WHERE bv.bulk_op_id = ANY($1::uuid[])
          AND NOT EXISTS (
                SELECT 1 FROM {TEMPLATE_VERSION_TABLE} pv
                WHERE pv.template_id = bv.template_id
                  AND pv.version = bv.version - 1
          )
        GROUP BY bv.bulk_op_id
    """
    return query, [bulk_op_ids]


def get_missing_preop_snapshots_query(bulk_op_id: str) -> Tuple[str, List[Any]]:
    """Template ids touched by this op whose pre-op snapshot (version - 1)
    was pruned by retention and so can't be restored. Used as an
    all-or-nothing pre-check inside ``bulk_rollback_templates`` — run BEFORE
    any restore write so a partially-prunable op aborts cleanly with zero
    writes instead of reverting some members and reporting others as errors.
    """
    query = f"""
        SELECT bv.template_id
        FROM {TEMPLATE_VERSION_TABLE} bv
        WHERE bv.bulk_op_id = $1
          AND NOT EXISTS (
                SELECT 1 FROM {TEMPLATE_VERSION_TABLE} pv
                WHERE pv.template_id = bv.template_id
                  AND pv.version = bv.version - 1
          )
    """
    return query, [bulk_op_id]


TEMPLATE_FAMILY_TABLE = "template_family"


_FAMILY_COLUMNS = (
    "id, name, description, flow, expected_payload_schema, "
    "expected_callback_response_schema, configurations, supported_channels, "
    "base_version, created_by, updated_by, created_at, updated_at"
)


def insert_template_family_query(
    family_id: str,
    name: str,
    description: Optional[str],
    flow_json: str,
    expected_payload_schema_json: Optional[str],
    expected_callback_response_schema_json: Optional[str],
    configurations_json: Optional[str],
    supported_channels: List[str],
    created_by: Optional[str],
) -> Tuple[str, List[Any]]:
    query = f"""
        INSERT INTO {TEMPLATE_FAMILY_TABLE}
            (id, name, description, flow,
             expected_payload_schema, expected_callback_response_schema,
             configurations, supported_channels, created_by)
        VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7::jsonb, $8, $9)
        RETURNING {_FAMILY_COLUMNS}
    """
    return query, [
        family_id,
        name,
        description,
        flow_json,
        expected_payload_schema_json,
        expected_callback_response_schema_json,
        configurations_json,
        supported_channels,
        created_by,
    ]


def update_template_family_query(
    family_id: str,
    name: str,
    description: Optional[str],
    flow_json: str,
    expected_payload_schema_json: Optional[str],
    expected_callback_response_schema_json: Optional[str],
    configurations_json: Optional[str],
    supported_channels: List[str],
    updated_by: Optional[str],
    now,
) -> Tuple[str, List[Any]]:
    """Edit the family's parent template content; bumps base_version."""
    query = f"""
        UPDATE {TEMPLATE_FAMILY_TABLE}
        SET name = $1,
            description = $2,
            flow = $3::jsonb,
            expected_payload_schema = $4::jsonb,
            expected_callback_response_schema = $5::jsonb,
            configurations = $6::jsonb,
            supported_channels = $7,
            base_version = base_version + 1,
            updated_by = $8,
            updated_at = $9
        WHERE id = $10
        RETURNING {_FAMILY_COLUMNS}
    """
    return query, [
        name,
        description,
        flow_json,
        expected_payload_schema_json,
        expected_callback_response_schema_json,
        configurations_json,
        supported_channels,
        updated_by,
        now,
        family_id,
    ]


def get_template_family_query(family_id: str) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_FAMILY_COLUMNS}
        FROM {TEMPLATE_FAMILY_TABLE}
        WHERE id = $1
        LIMIT 1
    """
    return query, [family_id]


def list_template_families_query() -> Tuple[str, List[Any]]:
    """List all families (global, admin-managed — no reseller scoping)."""
    query = f"""
        SELECT {_FAMILY_COLUMNS}
        FROM {TEMPLATE_FAMILY_TABLE}
        ORDER BY name
    """
    return query, []


def get_family_members_query(family_id: str) -> Tuple[str, List[Any]]:
    query = """
        SELECT id, name, reseller_id, merchant_id, current_version, derived_from_base_version
        FROM template
        WHERE family_id = $1
        ORDER BY name
    """
    return query, [family_id]


def count_family_members_query(family_ids: List[str]) -> Tuple[str, List[Any]]:
    """Member counts for a page of families in one round trip — the list
    endpoint only renders the count, so loading every member row per family
    was both an N+1 and a payload the caller throws away. Families with no
    members are absent from the result; the caller defaults them to 0."""
    query = """
        SELECT family_id, COUNT(*) AS member_count
        FROM template
        WHERE family_id = ANY($1::uuid[])
        GROUP BY family_id
    """
    return query, [family_ids]


def remove_family_members_query(
    family_id: str, template_ids: List[str], now
) -> Tuple[str, List[Any]]:
    query = """
        UPDATE template
        SET family_id = NULL,
            derived_from_base_version = NULL,
            updated_at = $2
        WHERE family_id = $1 AND id = ANY($3::uuid[])
        RETURNING id
    """
    return query, [family_id, now, template_ids]


TEMPLATE_FAMILY_VERSION_TABLE = "template_family_version"


_FAMILY_VERSION_COLUMNS = (
    "family_id, base_version, name, description, flow, "
    "expected_payload_schema, expected_callback_response_schema, "
    "configurations, supported_channels, changed_by, created_at"
)


def insert_template_family_version_query(
    family_id: str,
    base_version: int,
    name: str,
    description: Optional[str],
    flow_json: str,
    expected_payload_schema_json: Optional[str],
    expected_callback_response_schema_json: Optional[str],
    configurations_json: Optional[str],
    supported_channels: List[str],
    changed_by: Optional[str],
) -> Tuple[str, List[Any]]:
    """Append-only snapshot of the family's parent content AT base_version."""
    query = f"""
        INSERT INTO {TEMPLATE_FAMILY_VERSION_TABLE} (
            family_id, base_version, name, description, flow,
            expected_payload_schema, expected_callback_response_schema,
            configurations, supported_channels, changed_by
        )
        VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10)
        RETURNING id, family_id, base_version, changed_by, created_at
    """
    return query, [
        family_id,
        base_version,
        name,
        description,
        flow_json,
        expected_payload_schema_json,
        expected_callback_response_schema_json,
        configurations_json,
        supported_channels,
        changed_by,
    ]


def get_family_versions_query(
    family_id: str, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT family_id, base_version, name, changed_by, created_at
        FROM {TEMPLATE_FAMILY_VERSION_TABLE}
        WHERE family_id = $1
        ORDER BY base_version DESC
        LIMIT $2 OFFSET $3
    """
    return query, [family_id, limit, offset]


def get_family_versions_count_query(family_id: str) -> Tuple[str, List[Any]]:
    return (
        f"SELECT COUNT(*) AS total FROM {TEMPLATE_FAMILY_VERSION_TABLE} "
        "WHERE family_id = $1",
        [family_id],
    )


def get_family_version_snapshot_query(
    family_id: str, base_version: int
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT {_FAMILY_VERSION_COLUMNS}
        FROM {TEMPLATE_FAMILY_VERSION_TABLE}
        WHERE family_id = $1 AND base_version = $2
        LIMIT 1
    """
    return query, [family_id, base_version]


def prune_family_versions_query(
    family_id: str, base_version: int, keep: int
) -> Tuple[str, List[Any]]:
    """Retention: keep only the newest ``keep`` family versions.

    Guard: never deletes a version an active (completed, not yet rolled
    back) propagation op still needs — the version it moved children FROM
    (what ``also_revert_family`` restores, and what children's
    ``derived_from_base_version`` is reset to) and the version it moved
    them TO. Runs in the same transaction as the snapshot insert.
    """
    query = f"""
        DELETE FROM {TEMPLATE_FAMILY_VERSION_TABLE} fv
        WHERE fv.family_id = $1
          AND fv.base_version <= $2::integer - $3::integer
          AND NOT EXISTS (
                SELECT 1
                FROM {TEMPLATE_BULK_OP_TABLE} op
                WHERE op.family_id = fv.family_id
                  AND op.op_type = 'propagation'
                  AND op.status = 'completed'
                  AND fv.base_version IN (op.from_base_version, op.to_base_version)
          )
    """
    return query, [family_id, base_version, keep]


def get_template_family_for_update_query(family_id: str) -> Tuple[str, List[Any]]:
    """Lock the family row for a propagation apply, so a concurrent
    ``PUT /templates/families/{id}`` cannot bump base_version mid-merge."""
    query = f"""
        SELECT {_FAMILY_COLUMNS}
        FROM {TEMPLATE_FAMILY_TABLE}
        WHERE id = $1
        FOR UPDATE
    """
    return query, [family_id]
