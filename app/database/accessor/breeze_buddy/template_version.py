"""Accessors for template versioning: snapshot insert, reads, rollback."""

import json
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

from app.core.config.static import TEMPLATE_VERSION_RETENTION
from app.core.logger import logger
from app.database import db_connection
from app.database.decoder.breeze_buddy.template import (
    TemplateModel,
    decode_template,
)
from app.database.decoder.breeze_buddy.template_version import (
    decode_template_version_meta,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.template import restore_template_head_query
from app.database.queries.breeze_buddy.template_version import (
    get_template_version_snapshot_query,
    get_template_versions_count_query,
    get_template_versions_query,
    insert_template_version_query,
    prune_template_versions_query,
)
from app.schemas.breeze_buddy.template_version import TemplateVersionMetadata


def _as_json_str(value: Any) -> Optional[str]:
    """asyncpg returns JSONB as str; accept dict for direct-construction paths."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


async def insert_template_version_on_conn(
    conn,
    head_row: Any,
    change_source: str,
    bulk_op_id: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> None:
    """Snapshot a head-table RETURNING row as an immutable version.

    MUST run on the same connection/transaction as the head write so the
    head bump and its snapshot commit or roll back together.
    """
    query, values = insert_template_version_query(
        str(head_row["id"]),
        head_row["current_version"],
        head_row["reseller_id"],
        head_row["merchant_id"],
        head_row["name"],
        _as_json_str(head_row["flow"]) or "{}",
        _as_json_str(head_row["expected_payload_schema"]),
        _as_json_str(head_row["expected_callback_response_schema"]),
        _as_json_str(head_row["configurations"]),
        _as_json_str(head_row["secrets"]),
        (
            str(head_row["telephony_number_id"])
            if head_row["telephony_number_id"]
            else None
        ),
        head_row["is_active"],
        list(head_row["supported_channels"]),
        change_source,
        bulk_op_id,
        changed_by,
    )
    await conn.fetchrow(query, *values)
    # Retention: keep only the newest TEMPLATE_VERSION_RETENTION versions,
    # in the same transaction as the insert so the table self-maintains.
    prune_q, prune_v = prune_template_versions_query(
        str(head_row["id"]),
        head_row["current_version"],
        TEMPLATE_VERSION_RETENTION,
    )
    await conn.execute(prune_q, *prune_v)


async def list_template_versions(
    template_id: str,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[TemplateVersionMetadata], int]:
    """History for a template, scoped to the owner the caller was authorized
    against -- see ``_OWNER_SCOPE`` in the query module for why."""
    try:
        query, values = get_template_versions_query(
            template_id, owner_reseller_id, owner_merchant_id, limit, offset
        )
        rows = await run_parameterized_query(query, values)
        count_q, count_v = get_template_versions_count_query(
            template_id, owner_reseller_id, owner_merchant_id
        )
        count_rows = await run_parameterized_query(count_q, count_v)
        total = count_rows[0]["total"] if count_rows else 0
        return [decode_template_version_meta(r) for r in (rows or [])], total
    except Exception as e:
        logger.error(f"Error listing versions for template {template_id}: {e}")
        raise


async def get_template_version(
    template_id: str,
    version: int,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
) -> Optional[Tuple[TemplateModel, TemplateVersionMetadata]]:
    """One snapshot, or None when it does not exist OR was written under a
    previous owner of the template -- indistinguishable on purpose, so the
    caller gets a 404 either way rather than learning the version exists."""
    try:
        query, values = get_template_version_snapshot_query(
            template_id, version, owner_reseller_id, owner_merchant_id
        )
        rows = await run_parameterized_query(query, values)
        if not rows:
            return None
        row = rows[0]
        snapshot = decode_template(row)
        if snapshot is None:
            return None
        meta = decode_template_version_meta(
            {
                "template_id": row["id"],
                "version": row["version"],
                "change_source": row["change_source"],
                "bulk_op_id": row.get("bulk_op_id"),
                "changed_by": row.get("changed_by"),
                "created_at": row["created_at"],
            }
        )
        return snapshot, meta
    except Exception as e:
        logger.error(f"Error getting version {version} of template {template_id}: {e}")
        raise


async def _rollback_on_conn(
    conn,
    template_id: str,
    version: int,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
    changed_by: Optional[str],
    change_source: str,
    bulk_op_id: Optional[str],
) -> Optional[TemplateModel]:
    snap_q, snap_v = get_template_version_snapshot_query(
        template_id, version, owner_reseller_id, owner_merchant_id
    )
    snap = await conn.fetchrow(snap_q, *snap_v)
    if snap is None:
        return None
    restore_q, restore_v = restore_template_head_query(
        template_id,
        snap["name"],
        _as_json_str(snap["flow"]) or "{}",
        _as_json_str(snap["expected_payload_schema"]),
        _as_json_str(snap["expected_callback_response_schema"]),
        _as_json_str(snap["configurations"]),
        _as_json_str(snap["secrets"]),
        list(snap["supported_channels"]),
        datetime.now(timezone.utc),
        owner_reseller_id,
        owner_merchant_id,
    )
    # None here also means "the template moved owners since the handler
    # authorised this call" — the UPDATE re-checks, so it matched no row.
    # Same answer either way, which is what the reads already do.
    head = await conn.fetchrow(restore_q, *restore_v)
    if head is None:
        return None
    await insert_template_version_on_conn(
        conn,
        head,
        change_source=change_source,
        bulk_op_id=bulk_op_id,
        changed_by=changed_by,
    )
    return decode_template(head)


async def rollback_template_to_version(
    template_id: str,
    version: int,
    owner_reseller_id: str,
    owner_merchant_id: Optional[str],
    changed_by: Optional[str],
) -> Optional[TemplateModel]:
    """Restore an older snapshot as the new head (+ new version row).

    Owner-scoped like the reads: a snapshot written while the template
    belonged to a different reseller/merchant is not restorable, so a move
    cannot be used to pull a previous owner's content into the new one.

    Raises on DB conflicts (unique name, FK on telephony_number_id) so the
    handler can surface a 409 instead of silently half-applying.
    """
    try:
        async with db_connection() as db_conn:
            async with db_conn.transaction():
                return await _rollback_on_conn(
                    db_conn,
                    template_id,
                    version,
                    owner_reseller_id,
                    owner_merchant_id,
                    changed_by,
                    "rollback",
                    None,
                )
    except Exception as e:
        logger.error(
            f"Error rolling back template {template_id} to version {version}: {e}"
        )
        raise
