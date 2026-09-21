"""SQL queries for template versioning (lineage)."""

from typing import Any, List, Optional, Tuple

TEMPLATE_VERSION_TABLE = "template_version"

# Every read of history is scoped to the template's CURRENT owner.
#
# A snapshot records reseller_id/merchant_id as they were AT WRITE TIME, while
# authorization is checked against the head row. Without this predicate a
# template that moves reseller or merchant hands its entire previous owner's
# prompts and configurations to the new one. Rows written under a previous
# owner stay in the table for audit; they are simply not readable or
# restorable through the versions API.
#
# IS NOT DISTINCT FROM (not =) so a NULL merchant_id -- a reseller-level
# template -- matches NULL instead of never matching. All three readers bind
# the owner as $2/$3 so this fragment is positionally valid in each.
_OWNER_SCOPE = """
          AND reseller_id IS NOT DISTINCT FROM $2
          AND merchant_id IS NOT DISTINCT FROM $3"""


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
    template_id: str,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
    limit: int,
    offset: int,
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT template_id, version, change_source, bulk_op_id,
               changed_by, created_at
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1{_OWNER_SCOPE}
        ORDER BY version DESC
        LIMIT $4 OFFSET $5
    """
    return query, [template_id, owner_reseller_id, owner_merchant_id, limit, offset]


def get_template_versions_count_query(
    template_id: str, owner_reseller_id: str, owner_merchant_id: Optional[str]
) -> Tuple[str, List[Any]]:
    query = f"""
        SELECT COUNT(*) AS total
        FROM {TEMPLATE_VERSION_TABLE}
        WHERE template_id = $1{_OWNER_SCOPE}
    """
    return query, [template_id, owner_reseller_id, owner_merchant_id]


def get_template_version_snapshot_query(
    template_id: str,
    version: int,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
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
        WHERE template_id = $1{_OWNER_SCOPE}
          AND version = $4
        LIMIT 1
    """
    return query, [template_id, owner_reseller_id, owner_merchant_id, version]


def prune_template_versions_query(
    template_id: str, current_version: int, keep: int
) -> Tuple[str, List[Any]]:
    """Retention: keep only the newest ``keep`` versions of a template.

    Runs in the same transaction as the snapshot insert, so the table
    self-maintains -- no cron, no trigger.
    """
    query = f"""
        DELETE FROM {TEMPLATE_VERSION_TABLE} tv
        WHERE tv.template_id = $1
          AND tv.version <= $2::integer - $3::integer
    """
    return query, [template_id, current_version, keep]
