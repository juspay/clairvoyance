"""Accessors for template versioning: snapshot insert, reads, rollback."""

import copy
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uuid import uuid4

from app.ai.voice.agents.breeze_buddy.template.merge import (
    apply_merge_decisions,
    merge_family_into_child,
)
from app.ai.voice.agents.breeze_buddy.template.patch import (
    apply_template_patches,
    validate_patched_template,
)
from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    TemplateModel,
)
from app.ai.voice.agents.breeze_buddy.utils.secrets import (
    contains_masked_literal,
    mask_mcp_auth_secrets,
)
from app.core.config.static import TEMPLATE_VERSION_RETENTION
from app.core.logger import logger
from app.database import get_db_connection
from app.database.decoder.breeze_buddy.template import decode_template
from app.database.decoder.breeze_buddy.template_version import (
    decode_template_version_meta,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.template import (
    assign_family_query,
    count_family_member_contents_query,
    get_family_member_contents_query,
    get_family_templates_for_update_query,
    get_template_head_versions_query,
    get_templates_for_update_query,
    restore_template_head_query,
    set_template_derived_base_version_query,
    set_templates_derived_base_version_query,
    update_template_bulk_fields_query,
)
from app.database.queries.breeze_buddy.template_version import (
    count_family_members_query,
    count_missing_preop_snapshots_bulk_query,
    get_bulk_op_by_id_query,
    get_bulk_op_touched_versions_query,
    get_family_members_query,
    get_family_version_snapshot_query,
    get_family_versions_count_query,
    get_family_versions_query,
    get_missing_preop_snapshots_query,
    get_template_family_for_update_query,
    get_template_family_query,
    get_template_version_snapshot_query,
    get_template_versions_count_query,
    get_template_versions_query,
    insert_bulk_op_query,
    insert_template_family_query,
    insert_template_family_version_query,
    insert_template_version_query,
    list_bulk_ops_query,
    list_template_families_query,
    mark_bulk_op_rolled_back_query,
    prune_family_versions_query,
    prune_template_versions_query,
    remove_family_members_query,
    update_template_family_query,
)
from app.schemas.breeze_buddy.template_version import (
    BulkOpSummary,
    BulkRollbackResponse,
    BulkTemplateResult,
    BulkUpdateResponse,
    ChildMergeOutcome,
    FamilyMember,
    FamilyResponse,
    FamilyVersionMetadata,
    FamilyVersionSnapshot,
    MergeFieldChange,
    PropagationChildPreview,
    PropagationPreviewResponse,
    PropagationResolution,
    TemplateVersionMetadata,
)


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
    # Retention: keep only the newest TEMPLATE_VERSION_RETENTION versions.
    # Same transaction as the insert; the query's guard clause never prunes
    # snapshots an active bulk op still needs for rollback.
    prune_q, prune_v = prune_template_versions_query(
        str(head_row["id"]),
        head_row["current_version"],
        TEMPLATE_VERSION_RETENTION,
    )
    await conn.execute(prune_q, *prune_v)


async def list_template_versions(
    template_id: str, limit: int = 50, offset: int = 0
) -> Tuple[List[TemplateVersionMetadata], int]:
    try:
        query, values = get_template_versions_query(template_id, limit, offset)
        rows = await run_parameterized_query(query, values)
        count_q, count_v = get_template_versions_count_query(template_id)
        count_rows = await run_parameterized_query(count_q, count_v)
        total = count_rows[0]["total"] if count_rows else 0
        return [decode_template_version_meta(r) for r in (rows or [])], total
    except Exception as e:
        logger.error(f"Error listing versions for template {template_id}: {e}")
        raise


async def get_template_version(
    template_id: str, version: int
) -> Optional[Tuple[TemplateModel, TemplateVersionMetadata]]:
    try:
        query, values = get_template_version_snapshot_query(template_id, version)
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


class PreOpSnapshotVanished(Exception):
    """Raised inside ``bulk_rollback_templates``' transaction when a member's
    pre-op snapshot disappeared between the all-or-nothing pre-check and the
    restore loop (a concurrent template DELETE cascades its version rows).

    Must be a raise, not a return: a plain ``return`` inside
    ``async with conn.transaction()`` COMMITS, which would leave the already
    restored siblings written and the op marked rolled_back. Unwinding out of
    the transaction rolls the whole rollback back; the handler turns it into
    the same 409 the pruned-snapshot pre-check produces.
    """

    def __init__(self, template_id: str) -> None:
        super().__init__(template_id)
        self.template_id = template_id


async def _rollback_on_conn(
    conn,
    template_id: str,
    version: int,
    changed_by: Optional[str],
    change_source: str,
    bulk_op_id: Optional[str],
) -> Optional[TemplateModel]:
    snap_q, snap_v = get_template_version_snapshot_query(template_id, version)
    snap = await conn.fetchrow(snap_q, *snap_v)
    if snap is None:
        return None
    restore_q, restore_v = restore_template_head_query(
        template_id,
        snap["reseller_id"],
        snap["merchant_id"],
        snap["name"],
        _as_json_str(snap["flow"]) or "{}",
        _as_json_str(snap["expected_payload_schema"]),
        _as_json_str(snap["expected_callback_response_schema"]),
        _as_json_str(snap["configurations"]),
        _as_json_str(snap["secrets"]),
        (str(snap["telephony_number_id"]) if snap["telephony_number_id"] else None),
        snap["is_active"],
        list(snap["supported_channels"]),
        datetime.now(timezone.utc),
    )
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
    changed_by: Optional[str],
) -> Optional[TemplateModel]:
    """Restore an older snapshot as the new head (+ new version row).

    Raises on DB conflicts (unique name, FK on telephony_number_id) so the
    handler can surface a 409 instead of silently half-applying.
    """
    async for db_conn in get_db_connection():
        async with db_conn.transaction():
            return await _rollback_on_conn(
                db_conn, template_id, version, changed_by, "rollback", None
            )
    return None


def _assert_all_assigned(family_id: str, requested: List[str], rows: Any) -> List[str]:
    """``assign_family_query`` returns only the rows it actually adopted.

    Anything absent is either a missing/deleted template ID, or a template
    that already belongs to a *different* family (the query's WHERE clause
    excludes those).
    """
    assigned = {str(r["id"]) for r in (rows or [])}
    rejected = sorted(set(requested) - assigned)
    if rejected:
        raise ValueError(
            f"cannot add templates to family {family_id}: "
            f"{', '.join(rejected)} — template IDs are missing, deleted, "
            "or already assigned to a different family"
        )
    return sorted(assigned)


async def assign_family(family_id: str, template_ids: List[str]) -> List[str]:
    try:
        query, values = assign_family_query(
            family_id, template_ids, datetime.now(timezone.utc)
        )
        rows = await run_parameterized_query(query, values)
        return _assert_all_assigned(family_id, template_ids, rows)
    except Exception as e:
        logger.error(f"Error assigning family {family_id}: {e}")
        raise


def _json_col(row: Any, key: str) -> Optional[Dict[str, Any]]:
    value = row.get(key)
    if isinstance(value, str):
        return json.loads(value)
    return value


def _mask_family_configurations(
    configurations: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Round-trip ``configurations`` through ``ConfigurationModel`` (no
    ``reveal_secrets`` context) so MCP auth secrets (token/password/
    api_key_value) come back masked, mirroring how ``GET /templates/{id}``
    masks them. ``template_family.configurations`` is stored as a plain
    dict (unlike ``template``, which round-trips through the head-table
    decoder's ``ConfigurationModel``), so without this step a family read
    would serialize whatever was persisted verbatim -- including any
    plaintext secret that slipped in via a copy-from-template create.

    Falls back to the raw dict on validation failure (logged) so a legacy
    or malformed shape can't 500 a family read.
    """
    if configurations is None:
        return None
    try:
        model = ConfigurationModel.model_validate(configurations)
        return model.model_dump(exclude_none=True, mode="json")
    except Exception as exc:
        logger.warning(
            f"Failed to mask family configurations, falling back to raw dict: {exc}"
        )
        return configurations


def _decode_family(
    row: Any, members: List[FamilyMember], member_count: Optional[int] = None
) -> FamilyResponse:
    return FamilyResponse(
        id=str(row["id"]),
        name=row["name"],
        description=row.get("description"),
        flow=_json_col(row, "flow") or {},
        expected_payload_schema=_json_col(row, "expected_payload_schema"),
        expected_callback_response_schema=_json_col(
            row, "expected_callback_response_schema"
        ),
        configurations=_mask_family_configurations(_json_col(row, "configurations")),
        supported_channels=list(row.get("supported_channels") or ["voice"]),
        base_version=row.get("base_version") or 1,
        members=members,
        member_count=len(members) if member_count is None else member_count,
        created_by=row.get("created_by"),
        updated_by=row.get("updated_by"),
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


async def insert_family_version_on_conn(
    conn, family_row: Any, changed_by: Optional[str]
) -> None:
    """Snapshot a template_family RETURNING row as an immutable family version.

    MUST run on the same connection/transaction as the family write. The row
    is copied verbatim: template_family holds no secrets column and its
    configurations are mask-level by construction, so the snapshot carries
    exactly the same trust level as the family row itself.
    """
    query, values = insert_template_family_version_query(
        str(family_row["id"]),
        family_row["base_version"],
        family_row["name"],
        family_row.get("description"),
        _as_json_str(family_row["flow"]) or "{}",
        _as_json_str(family_row["expected_payload_schema"]),
        _as_json_str(family_row["expected_callback_response_schema"]),
        _as_json_str(family_row["configurations"]),
        list(family_row["supported_channels"] or ["voice"]),
        changed_by,
    )
    await conn.fetchrow(query, *values)
    prune_q, prune_v = prune_family_versions_query(
        str(family_row["id"]),
        family_row["base_version"],
        TEMPLATE_VERSION_RETENTION,
    )
    await conn.execute(prune_q, *prune_v)


def _decode_family_member(row: Any) -> FamilyMember:
    return FamilyMember(
        template_id=str(row["id"]),
        name=row["name"],
        reseller_id=row["reseller_id"],
        merchant_id=row.get("merchant_id"),
        current_version=row.get("current_version") or 1,
        derived_from_base_version=row.get("derived_from_base_version"),
    )


async def _load_family_members_on_conn(conn, family_id: str) -> List[FamilyMember]:
    """Same as ``_load_family_members`` but on an open connection — required
    inside a transaction, where a second pooled connection would not yet see
    the uncommitted membership changes."""
    query, values = get_family_members_query(family_id)
    rows = await conn.fetch(query, *values)
    return [_decode_family_member(r) for r in (rows or [])]


async def _load_family_members(family_id: str) -> List[FamilyMember]:
    query, values = get_family_members_query(family_id)
    rows = await run_parameterized_query(query, values)
    return [_decode_family_member(r) for r in (rows or [])]


async def create_template_family(
    *,
    name: str,
    description: Optional[str],
    flow: Dict[str, Any],
    expected_payload_schema: Optional[Dict[str, Any]],
    expected_callback_response_schema: Optional[Dict[str, Any]],
    configurations: Optional[Dict[str, Any]],
    supported_channels: List[str],
    member_template_ids: List[str],
    created_by: Optional[str],
) -> Optional[FamilyResponse]:
    family_id = str(uuid4())
    # Write-time enforcement of the "family content never holds a real
    # secret" invariant: masking on read alone would still leave a token
    # supplied inline by an admin at rest in template_family and in every
    # template_family_version snapshot.
    configurations = mask_mcp_auth_secrets(configurations)
    query, values = insert_template_family_query(
        family_id,
        name,
        description,
        json.dumps(flow),
        (
            json.dumps(expected_payload_schema)
            if expected_payload_schema is not None
            else None
        ),
        (
            json.dumps(expected_callback_response_schema)
            if expected_callback_response_schema is not None
            else None
        ),
        json.dumps(configurations) if configurations is not None else None,
        supported_channels,
        created_by,
    )
    async for conn in get_db_connection():
        async with conn.transaction():
            row = await conn.fetchrow(query, *values)
            if row is None:
                return None
            # base_version 1 must exist as a snapshot: it is the merge base
            # for any child that derives from the family's first revision.
            await insert_family_version_on_conn(conn, row, created_by)
            if member_template_ids:
                assign_q, assign_v = assign_family_query(
                    family_id, member_template_ids, datetime.now(timezone.utc)
                )
                # Raises out of the transaction (so the family is not created
                # either) if any requested member is missing or belongs to a
                # different reseller.
                _assert_all_assigned(
                    family_id,
                    member_template_ids,
                    await conn.fetch(assign_q, *assign_v),
                )
            return _decode_family(
                row, await _load_family_members_on_conn(conn, family_id)
            )
    return None


async def update_template_family(
    family_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    flow: Optional[Dict[str, Any]] = None,
    expected_payload_schema: Optional[Dict[str, Any]] = None,
    expected_callback_response_schema: Optional[Dict[str, Any]] = None,
    configurations: Optional[Dict[str, Any]] = None,
    supported_channels: Optional[List[str]] = None,
    updated_by: Optional[str] = None,
) -> Optional[FamilyResponse]:
    """``None`` for any field means "leave that column as it is".

    Unchanged columns are carried over from the row locked here, NOT from a
    decoded ``FamilyResponse``: the decoder round-trips configurations
    through ``ConfigurationModel``, which materialises every Pydantic
    default, so echoing that back would rewrite the stored configuration
    with keys the caller never set — and snapshot them into history — on a
    PUT that only changed the name. Reading under ``FOR UPDATE`` also makes
    the read-modify-write atomic against a concurrent edit.
    """
    # Same invariant as create_template_family: never persist a real secret
    # into the family row or its snapshot.
    configurations = mask_mcp_auth_secrets(configurations)
    async for conn in get_db_connection():
        async with conn.transaction():
            lock_q, lock_v = get_template_family_for_update_query(family_id)
            current = await conn.fetchrow(lock_q, *lock_v)
            if current is None:
                return None
            query, values = update_template_family_query(
                family_id,
                name if name is not None else current["name"],
                description if description is not None else current["description"],
                (
                    json.dumps(flow)
                    if flow is not None
                    else (_as_json_str(current["flow"]) or "{}")
                ),
                (
                    json.dumps(expected_payload_schema)
                    if expected_payload_schema is not None
                    else _as_json_str(current["expected_payload_schema"])
                ),
                (
                    json.dumps(expected_callback_response_schema)
                    if expected_callback_response_schema is not None
                    else _as_json_str(current["expected_callback_response_schema"])
                ),
                (
                    json.dumps(configurations)
                    if configurations is not None
                    else _as_json_str(current["configurations"])
                ),
                (
                    supported_channels
                    if supported_channels is not None
                    else list(current["supported_channels"] or ["voice"])
                ),
                updated_by,
                datetime.now(timezone.utc),
            )
            row = await conn.fetchrow(query, *values)
            if row is None:
                return None
            # Same transaction as the base_version bump: head and history
            # can never diverge (docs/TEMPLATE_LINEAGE.md §4).
            await insert_family_version_on_conn(conn, row, updated_by)
            return _decode_family(
                row, await _load_family_members_on_conn(conn, family_id)
            )
    return None


async def get_template_family(family_id: str) -> Optional[FamilyResponse]:
    query, values = get_template_family_query(family_id)
    rows = await run_parameterized_query(query, values)
    if not rows:
        return None
    return _decode_family(rows[0], await _load_family_members(family_id))


async def list_template_families() -> List[FamilyResponse]:
    """List all families WITHOUT their member rows (global, admin-only).

    The listing carries ``member_count`` only: loading every member of every
    family was one query per family (N+1) plus a member list the list screen
    never renders. ``GET /templates/families/{id}`` remains the place that
    returns the full ``members`` array.
    """
    query, values = list_template_families_query()
    rows = await run_parameterized_query(query, values)
    if not rows:
        return []
    count_q, count_v = count_family_members_query([str(r["id"]) for r in rows])
    counts = {
        str(c["family_id"]): c["member_count"]
        for c in (await run_parameterized_query(count_q, count_v) or [])
    }
    return [_decode_family(r, [], counts.get(str(r["id"]), 0)) for r in rows]


async def update_family_members(
    family_id: str, add: List[str], remove: List[str]
) -> Optional[FamilyResponse]:
    if add:
        await assign_family(family_id, add)
    if remove:
        query, values = remove_family_members_query(
            family_id, remove, datetime.now(timezone.utc)
        )
        await run_parameterized_query(query, values)
    return await get_template_family(family_id)


async def bulk_update_templates(
    *,
    template_ids: Optional[List[str]],
    family_id: Optional[str],
    flow_patch: Optional[Dict[str, Any]],
    node_patches: Optional[Dict[str, Dict[str, Any]]],
    configurations_patch: Optional[Dict[str, Any]],
    patch_json: str,
    changed_by: Optional[str],
    dry_run: bool,
) -> BulkUpdateResponse:
    """All-or-nothing bulk patch. Each member's OWN current state is patched
    (merge patch preserves everything the patch doesn't mention), validated,
    then written + snapshotted under one bulk_op_id in one transaction."""
    if configurations_patch is not None and contains_masked_literal(
        configurations_patch
    ):
        # Same rule the propagation "custom" resolution enforces: a client
        # that built this patch from a masked read (GET /templates/{id} masks
        # MCP auth secrets) would otherwise persist the literal "**********"
        # over every member's real token and break MCP auth at runtime. The
        # merge-patch semantics already mean "omit the field to keep the
        # existing value", so there is nothing to substitute here.
        return BulkUpdateResponse(
            status="failed",
            results=[
                BulkTemplateResult(
                    template_id="",
                    name="",
                    error=(
                        "configurations_patch contains a masked secret "
                        "placeholder; omit the field to keep each template's "
                        "existing value, or send the real one"
                    ),
                )
            ],
        )
    async for conn in get_db_connection():
        async with conn.transaction():
            if family_id:
                lock_q, lock_v = get_family_templates_for_update_query(family_id)
            else:
                lock_q, lock_v = get_templates_for_update_query(template_ids or [])
            members = await conn.fetch(lock_q, *lock_v)
            if template_ids:
                # All-or-nothing: an ID that no longer exists must fail the
                # op, not silently drop out of it and report "completed" for
                # the rest.
                found = {str(r["id"]) for r in members}
                missing = [tid for tid in template_ids if tid not in found]
                if missing:
                    return BulkUpdateResponse(
                        status="failed",
                        results=[
                            BulkTemplateResult(
                                template_id=tid,
                                name="",
                                error="template not found",
                            )
                            for tid in missing
                        ],
                    )
            if not members:
                return BulkUpdateResponse(
                    status="failed",
                    results=[
                        BulkTemplateResult(
                            template_id="",
                            name="",
                            error=f"family {family_id} has no member templates",
                        )
                    ],
                )

            patched: List[Tuple[Any, Dict[str, Any], Optional[Dict[str, Any]]]] = []
            errors: List[BulkTemplateResult] = []
            for row in members:
                flow = (
                    json.loads(row["flow"])
                    if isinstance(row["flow"], str)
                    else dict(row["flow"] or {})
                )
                configurations = row["configurations"]
                if isinstance(configurations, str):
                    configurations = json.loads(configurations)
                try:
                    new_flow, new_configurations = apply_template_patches(
                        flow,
                        configurations,
                        flow_patch,
                        node_patches,
                        configurations_patch,
                    )
                    err = validate_patched_template(
                        new_flow, new_configurations, str(row["id"]), row["name"]
                    )
                except ValueError as exc:
                    err = str(exc)
                    new_flow, new_configurations = flow, configurations
                if err:
                    errors.append(
                        BulkTemplateResult(
                            template_id=str(row["id"]),
                            name=row["name"],
                            from_version=row["current_version"],
                            error=err,
                        )
                    )
                else:
                    patched.append((row, new_flow, new_configurations))

            if errors:
                # abort: report every error, write nothing
                return BulkUpdateResponse(status="failed", results=errors)

            if dry_run:
                return BulkUpdateResponse(
                    status="dry_run",
                    results=[
                        BulkTemplateResult(
                            template_id=str(row["id"]),
                            name=row["name"],
                            from_version=row["current_version"],
                            to_version=row["current_version"] + 1,
                            preview_flow=new_flow,
                            preview_configurations=_mask_family_configurations(
                                new_configurations
                            ),
                        )
                        for row, new_flow, new_configurations in patched
                    ],
                )

            op_id = str(uuid4())
            op_q, op_v = insert_bulk_op_query(
                op_id,
                "bulk_update",
                family_id,
                [str(r["id"]) for r, _, _ in patched],
                patch_json,
                "completed",
                None,
                changed_by,
            )
            op_row = await conn.fetchrow(op_q, *op_v)

            results: List[BulkTemplateResult] = []
            now = datetime.now(timezone.utc)
            for row, new_flow, new_configurations in patched:
                upd_q, upd_v = update_template_bulk_fields_query(
                    str(row["id"]),
                    json.dumps(new_flow),
                    (
                        json.dumps(new_configurations)
                        if new_configurations is not None
                        else None
                    ),
                    now,
                )
                head = await conn.fetchrow(upd_q, *upd_v)
                await insert_template_version_on_conn(
                    conn,
                    head,
                    change_source="bulk_update",
                    bulk_op_id=op_id,
                    changed_by=changed_by,
                )
                results.append(
                    BulkTemplateResult(
                        template_id=str(row["id"]),
                        name=row["name"],
                        from_version=row["current_version"],
                        to_version=head["current_version"],
                    )
                )
            return BulkUpdateResponse(
                status="completed", bulk_op_id=str(op_row["id"]), results=results
            )
    return BulkUpdateResponse(status="failed", results=[])


async def bulk_rollback_templates(
    bulk_op_id: str,
    changed_by: Optional[str],
    force: bool,
    also_revert_family: bool = False,
    op: Optional[BulkOpSummary] = None,
) -> BulkRollbackResponse:
    """Revert every template touched by a bulk_update / propagation op to its
    pre-op snapshot (version - 1), as new versions under a new bulk_rollback
    op.

    ``op`` is the ledger row the handler already loaded for its 404/409
    preconditions; passing it avoids a second read and keeps the reverted
    children's ``derived_from_base_version`` correct for propagation ops.
    When it is None only the plain content revert runs.
    """

    def _fail(results: List[BulkTemplateResult]) -> BulkRollbackResponse:
        return BulkRollbackResponse(
            status="failed",
            bulk_op_id="",
            reverted_bulk_op_id=bulk_op_id,
            results=results,
        )

    async for conn in get_db_connection():
        async with conn.transaction():
            touched_q, touched_v = get_bulk_op_touched_versions_query(bulk_op_id)
            touched = await conn.fetch(touched_q, *touched_v)
            if not touched:
                return _fail([])

            head_q, head_v = get_template_head_versions_query(
                [str(t["template_id"]) for t in touched]
            )
            heads = {
                str(r["id"]): r["current_version"]
                for r in await conn.fetch(head_q, *head_v)
            }

            drifted = [
                BulkTemplateResult(
                    template_id=str(t["template_id"]),
                    name="",
                    from_version=heads.get(str(t["template_id"])),
                    error=(
                        f"drifted: head is v{heads.get(str(t['template_id']))}, "
                        f"bulk op wrote v{t['version']}"
                    ),
                )
                for t in touched
                if heads.get(str(t["template_id"])) != t["version"]
            ]
            if drifted and not force:
                return _fail(drifted)

            # All-or-nothing pre-check: verify every touched member's pre-op
            # snapshot still exists BEFORE writing anything. Without this, a
            # member whose snapshot was pruned by retention would silently
            # report "pre-op snapshot missing" while its siblings were
            # already restored and the op was still marked rolled_back --
            # dropping retention protection for the whole op on a partial
            # failure. force=true overrides the drift guard above but never
            # this: there's nothing to restore for a pruned snapshot.
            missing_q, missing_v = get_missing_preop_snapshots_query(bulk_op_id)
            missing_rows = await conn.fetch(missing_q, *missing_v)
            if missing_rows:
                missing_ids = {str(r["template_id"]) for r in missing_rows}
                return _fail(
                    [
                        BulkTemplateResult(
                            template_id=str(t["template_id"]),
                            name="",
                            from_version=heads.get(str(t["template_id"])),
                            error="pre-op snapshot missing (pruned)",
                        )
                        for t in touched
                        if str(t["template_id"]) in missing_ids
                    ]
                )

            is_propagation = op is not None and op.op_type == "propagation"
            family_target: Optional[int] = None
            if op is not None and is_propagation:
                if op.family_id is not None and op.from_base_version is not None:
                    family_target = op.from_base_version
            if also_revert_family:
                # All-or-nothing pre-check, mirroring the pruned-snapshot one:
                # a return inside the transaction still COMMITS, so the family
                # snapshot must be proven to exist before anything is written.
                if op is None or not is_propagation or family_target is None:
                    return _fail(
                        [
                            BulkTemplateResult(
                                template_id="",
                                name="",
                                error=(
                                    "also_revert_family is only valid for a "
                                    "propagation op with a from_base_version"
                                ),
                            )
                        ]
                    )
                # Lock the family row now, in this transaction, so a
                # concurrent PUT /templates/families/{id} cannot interleave
                # with the restore below (same pattern as
                # apply_family_propagation).
                lock_q, lock_v = get_template_family_for_update_query(str(op.family_id))
                await conn.fetchrow(lock_q, *lock_v)
                probe_q, probe_v = get_family_version_snapshot_query(
                    str(op.family_id), family_target
                )
                if await conn.fetchrow(probe_q, *probe_v) is None:
                    return _fail(
                        [
                            BulkTemplateResult(
                                template_id="",
                                name="",
                                error=(
                                    f"family snapshot missing: family "
                                    f"{op.family_id} base_version {family_target} "
                                    "was pruned by retention"
                                ),
                            )
                        ]
                    )

            new_op_id = str(uuid4())
            op_q, op_v = insert_bulk_op_query(
                new_op_id,
                "bulk_rollback",
                None,
                [str(t["template_id"]) for t in touched],
                None,
                "completed",
                bulk_op_id,
                changed_by,
            )
            op_row = await conn.fetchrow(op_q, *op_v)

            results: List[BulkTemplateResult] = []
            for t in touched:
                tid = str(t["template_id"])
                restored = await _rollback_on_conn(
                    conn,
                    tid,
                    t["version"] - 1,
                    changed_by,
                    change_source="bulk_rollback",
                    bulk_op_id=new_op_id,
                )
                if restored is None:
                    # The pre-check above proved every snapshot existed, so
                    # this means the template was deleted between the touched
                    # read and the FOR UPDATE lock (its version rows cascade),
                    # which force=true no longer guards against. Abort the
                    # whole transaction rather than reporting "completed" on a
                    # partial restore.
                    raise PreOpSnapshotVanished(tid)
                results.append(
                    BulkTemplateResult(
                        template_id=tid,
                        name=restored.name,
                        from_version=heads.get(tid),
                        to_version=restored.current_version,
                    )
                )

            family_reverted_to: Optional[int] = None
            if op is not None and is_propagation:
                # The children now carry pre-propagation content again, so
                # their sync pointer must go back too — otherwise the next
                # merge would use the wrong base.
                derived_target = op.from_base_version
                if also_revert_family and family_target is not None:
                    restored_family = await _rollback_family_on_conn(
                        conn, str(op.family_id), family_target, changed_by
                    )
                    family_reverted_to = family_target
                    if restored_family is not None:
                        # The restore appended a NEW family revision whose
                        # content equals from_base_version. Children match it
                        # exactly, so they derive from IT — pointing them at
                        # the old number would flag every member as "behind"
                        # a parent they are identical to.
                        derived_target = restored_family.base_version
                # op.template_ids, not `touched`: the propagation advanced the
                # pointer for every child it covered, including those whose
                # content the merge left unchanged (no version row, so absent
                # from `touched`). Resetting only the touched ones would leave
                # the unchanged children claiming to derive from a family
                # revision the rollback just undid, and the next propagation
                # would merge them against the wrong base.
                reset_q, reset_v = set_templates_derived_base_version_query(
                    op.template_ids, derived_target
                )
                await conn.execute(reset_q, *reset_v)

            mark_q, mark_v = mark_bulk_op_rolled_back_query(bulk_op_id)
            await conn.fetchrow(mark_q, *mark_v)

            return BulkRollbackResponse(
                status="completed",
                bulk_op_id=str(op_row["id"]),
                reverted_bulk_op_id=bulk_op_id,
                results=results,
                family_reverted_to_base_version=family_reverted_to,
            )
    return _fail([])


def _decode_bulk_op(row: Any) -> BulkOpSummary:
    return BulkOpSummary(
        id=str(row["id"]),
        op_type=row["op_type"],
        family_id=(str(row["family_id"]) if row.get("family_id") else None),
        template_ids=[str(t) for t in row["template_ids"]],
        status=row["status"],
        reverted_bulk_op_id=(
            str(row["reverted_bulk_op_id"]) if row.get("reverted_bulk_op_id") else None
        ),
        initiated_by=row.get("initiated_by"),
        created_at=row["created_at"],
        from_base_version=row.get("from_base_version"),
        to_base_version=row.get("to_base_version"),
    )


async def get_bulk_op(bulk_op_id: str) -> Optional[BulkOpSummary]:
    try:
        query, values = get_bulk_op_by_id_query(bulk_op_id)
        rows = await run_parameterized_query(query, values)
        return _decode_bulk_op(rows[0]) if rows else None
    except Exception as e:
        logger.error(f"Error loading bulk op {bulk_op_id}: {e}")
        raise


async def list_bulk_ops(limit: int = 50, offset: int = 0) -> List[BulkOpSummary]:
    try:
        query, values = list_bulk_ops_query(limit, offset)
        rows = await run_parameterized_query(query, values)
        ops = [_decode_bulk_op(r) for r in (rows or [])]
        # Only completed bulk_update ops are rollback candidates; the count
        # tells the UI how many of that op's members already lost their
        # pre-op snapshot to retention and can't be rolled back. One
        # aggregate query for the whole page instead of one query per op.
        rollback_candidate_ids = [
            op.id
            for op in ops
            if op.op_type in ("bulk_update", "propagation") and op.status == "completed"
        ]
        if rollback_candidate_ids:
            count_q, count_v = count_missing_preop_snapshots_bulk_query(
                rollback_candidate_ids
            )
            count_rows = await run_parameterized_query(count_q, count_v)
            missing_by_op = {
                str(r["bulk_op_id"]): r["missing"] for r in (count_rows or [])
            }
            candidate_ids = set(rollback_candidate_ids)
            for op in ops:
                if op.id in candidate_ids:
                    # absent from missing_by_op == zero missing snapshots
                    op.rollback_unavailable_count = missing_by_op.get(op.id, 0)
        return ops
    except Exception as e:
        logger.error(f"Error listing bulk ops: {e}")
        raise


def _decode_family_version_meta(row: Any) -> FamilyVersionMetadata:
    return FamilyVersionMetadata(
        family_id=str(row["family_id"]),
        base_version=row["base_version"],
        name=row["name"],
        changed_by=row.get("changed_by"),
        created_at=row["created_at"],
    )


async def list_family_versions(
    family_id: str, limit: int = 50, offset: int = 0
) -> Tuple[List[FamilyVersionMetadata], int]:
    try:
        query, values = get_family_versions_query(family_id, limit, offset)
        rows = await run_parameterized_query(query, values)
        count_q, count_v = get_family_versions_count_query(family_id)
        count_rows = await run_parameterized_query(count_q, count_v)
        total = count_rows[0]["total"] if count_rows else 0
        return [_decode_family_version_meta(r) for r in (rows or [])], total
    except Exception as e:
        logger.error(f"Error listing versions for family {family_id}: {e}")
        raise


def _decode_family_version_snapshot(row: Any) -> FamilyVersionSnapshot:
    return FamilyVersionSnapshot(
        name=row["name"],
        description=row.get("description"),
        flow=_json_col(row, "flow") or {},
        expected_payload_schema=_json_col(row, "expected_payload_schema"),
        expected_callback_response_schema=_json_col(
            row, "expected_callback_response_schema"
        ),
        # Same masking as _decode_family: a family version is read back
        # through ConfigurationModel with no reveal_secrets context.
        configurations=_mask_family_configurations(_json_col(row, "configurations")),
        supported_channels=list(row.get("supported_channels") or ["voice"]),
    )


async def get_family_version(
    family_id: str, base_version: int
) -> Optional[Tuple[FamilyVersionSnapshot, FamilyVersionMetadata]]:
    try:
        query, values = get_family_version_snapshot_query(family_id, base_version)
        rows = await run_parameterized_query(query, values)
        if not rows:
            return None
        row = rows[0]
        return _decode_family_version_snapshot(row), _decode_family_version_meta(row)
    except Exception as e:
        logger.error(
            f"Error getting base_version {base_version} of family {family_id}: {e}"
        )
        raise


async def _rollback_family_on_conn(
    conn, family_id: str, base_version: int, changed_by: Optional[str]
) -> Optional[FamilyResponse]:
    snap_q, snap_v = get_family_version_snapshot_query(family_id, base_version)
    snap = await conn.fetchrow(snap_q, *snap_v)
    if snap is None:
        return None
    # Restoring goes through the normal family UPDATE, which bumps
    # base_version: the restore is a NEW revision, never history surgery.
    upd_q, upd_v = update_template_family_query(
        family_id,
        snap["name"],
        snap.get("description"),
        _as_json_str(snap["flow"]) or "{}",
        _as_json_str(snap["expected_payload_schema"]),
        _as_json_str(snap["expected_callback_response_schema"]),
        _as_json_str(snap["configurations"]),
        list(snap["supported_channels"] or ["voice"]),
        changed_by,
        datetime.now(timezone.utc),
    )
    row = await conn.fetchrow(upd_q, *upd_v)
    if row is None:
        return None
    await insert_family_version_on_conn(conn, row, changed_by)
    return _decode_family(row, await _load_family_members_on_conn(conn, family_id))


async def rollback_family_to_version(
    family_id: str,
    base_version: int,
    changed_by: Optional[str],
) -> Optional[FamilyResponse]:
    """Restore an older family snapshot as a NEW base_version.

    Returns None when the requested snapshot does not exist.
    """
    async for db_conn in get_db_connection():
        async with db_conn.transaction():
            return await _rollback_family_on_conn(
                db_conn, family_id, base_version, changed_by
            )
    return None


async def _resolve_merge_base(
    fetch_snapshot,
    family_id: str,
    child_derived: Optional[int],
    previous_base_version: Optional[int],
    parent_flow: Dict[str, Any],
    parent_configurations: Optional[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], Optional[int], str]:
    """Pick the family snapshot the three-way merge uses as its base.

    Order:
      1. the family version the child last synced from
         (``derived_from_base_version``);
      2. the family version immediately before the one being propagated —
         used both when the child has never been propagated to
         (``derived_from_base_version IS NULL``) and when its snapshot was
         pruned by retention. A never-synced child can only honestly be
         claimed to be in sync with everything EXCEPT the edit being
         propagated right now: basing on family v1 would replay every
         historical family change as a conflict against a merchant who
         never opted into them, and basing on the child's own content would
         make every field a no-op and propagate nothing;
      3. the parent content itself — no base is available at all, so the
         merge sees parent == base and reports nothing. Safe by
         construction: a child is never silently overwritten.

    ``fetch_snapshot`` is ``async (family_id, base_version) -> Optional[row]``
    so preview (pooled read) and apply (inside the transaction) share this
    logic.
    """
    for candidate, source in (
        (child_derived, "derived"),
        (previous_base_version, "previous_version"),
    ):
        if candidate is None or candidate < 1:
            continue
        row = await fetch_snapshot(family_id, candidate)
        if row is not None:
            return (
                _json_col(row, "flow") or {},
                _json_col(row, "configurations"),
                candidate,
                source,
            )
    return (
        copy.deepcopy(parent_flow),
        copy.deepcopy(parent_configurations),
        None,
        "parent_fallback",
    )


def _mask_preview_configuration_values(
    outcome: ChildMergeOutcome,
    base_configurations: Optional[Dict[str, Any]],
    parent_configurations: Optional[Dict[str, Any]],
    child_configurations: Optional[Dict[str, Any]],
) -> None:
    """Mask every ``configurations.*`` value that leaves the accessor:
    conflicts' ``base``/``parent``/``child`` AND every ``auto_applied``
    entry's ``value``.

    Children (unlike families) hold real MCP auth secrets. Family-side
    values are *supposed* to already be mask-level (``template_family`` is
    round-tripped through ``ConfigurationModel`` on every write), but
    ``_mask_family_configurations``'s own docstring documents a path for a
    plaintext secret to slip in anyway (a copy-from-template create before
    that masking existed, or a legacy row) — so base/parent are re-masked
    here too, for defense in depth. The merge itself already ran on the raw
    values, so classification stays correct; only what leaves the accessor
    is masked.
    """
    masked_base = _mask_family_configurations(base_configurations) or {}
    masked_parent = _mask_family_configurations(parent_configurations) or {}
    masked_child = _mask_family_configurations(child_configurations) or {}

    def _key(field_path: str) -> str:
        return field_path.split(".", 1)[1]

    for conflict in outcome.conflicts:
        if not conflict.field_path.startswith("configurations."):
            continue
        key = _key(conflict.field_path)
        if conflict.base_present and key in masked_base:
            conflict.base = masked_base[key]
        if conflict.parent_present and key in masked_parent:
            conflict.parent = masked_parent[key]
        if conflict.child_present and key in masked_child:
            conflict.child = masked_child[key]

    for change in outcome.auto_apply:
        # auto_apply's ``value`` is always the parent-side value (see
        # merge.py's _classify: the branch only fires when child == base,
        # so the change carried forward is parent's), except for
        # op="remove" where value is already None.
        if change.op != "set" or not change.field_path.startswith("configurations."):
            continue
        key = _key(change.field_path)
        if key in masked_parent:
            change.value = masked_parent[key]


async def preview_family_propagation(
    family_id: str,
    page: int = 1,
    limit: int = 50,
) -> Optional[PropagationPreviewResponse]:
    """Three-way merge the family's current content into one page of children.

    Writes nothing, locks nothing (docs/TEMPLATE_LINEAGE.md §6.3 step 2).

    ``page`` / ``limit`` enable streaming large families: the UI fetches
    successive pages, accumulates the per-child data, and defers apply until
    all pages are loaded and every conflict is resolved.
    """
    try:
        fam_q, fam_v = get_template_family_query(family_id)
        fam_rows = await run_parameterized_query(fam_q, fam_v)
        if not fam_rows:
            return None
        family = fam_rows[0]
        to_base_version = family.get("base_version") or 1
        previous_base_version = to_base_version - 1 if to_base_version > 1 else None
        parent_flow = _json_col(family, "flow") or {}
        parent_configurations = _json_col(family, "configurations")

        # Total member count for pagination metadata
        count_q, count_v = count_family_member_contents_query(family_id)
        count_rows = await run_parameterized_query(count_q, count_v)
        total_members = int(count_rows[0]["total"]) if count_rows else 0

        offset = (page - 1) * limit
        members_q, members_v = get_family_member_contents_query(
            family_id, limit=limit, offset=offset
        )
        members = await run_parameterized_query(members_q, members_v)

        async def fetch_snapshot(fid: str, base_version: int):
            q, v = get_family_version_snapshot_query(fid, base_version)
            rows = await run_parameterized_query(q, v)
            return rows[0] if rows else None

        children: List[PropagationChildPreview] = []
        conflict_count = 0
        expected_current_versions: Dict[str, int] = {}
        for row in members or []:
            template_id = str(row["id"])
            current_version = row.get("current_version") or 1
            expected_current_versions[template_id] = current_version
            derived = row.get("derived_from_base_version")
            base_flow, base_configurations, base_version_used, base_source = (
                await _resolve_merge_base(
                    fetch_snapshot,
                    family_id,
                    derived,
                    previous_base_version,
                    parent_flow,
                    parent_configurations,
                )
            )
            child_configurations = _json_col(row, "configurations")
            preview = PropagationChildPreview(
                template_id=template_id,
                name=row["name"],
                merchant_id=row.get("merchant_id"),
                current_version=current_version,
                derived_from_base_version=derived,
                merge_base_version=base_version_used,
                base_source=base_source,
            )
            try:
                outcome = merge_family_into_child(
                    base_flow=base_flow,
                    base_configurations=base_configurations,
                    parent_flow=parent_flow,
                    parent_configurations=parent_configurations,
                    child_flow=_json_col(row, "flow") or {},
                    child_configurations=child_configurations,
                )
            except ValueError as exc:
                preview.error = str(exc)
                children.append(preview)
                continue
            _mask_preview_configuration_values(
                outcome,
                base_configurations,
                parent_configurations,
                child_configurations,
            )
            preview.auto_applied = outcome.auto_apply
            preview.noop = outcome.noop
            preview.conflicts = outcome.conflicts
            conflict_count += len(outcome.conflicts)
            children.append(preview)

        has_more = (offset + len(members or [])) < total_members

        return PropagationPreviewResponse(
            family_id=family_id,
            to_base_version=to_base_version,
            from_base_version=previous_base_version,
            children=children,
            conflict_count=conflict_count,
            expected_current_versions=expected_current_versions,
            total_members=total_members,
            page=page,
            limit=limit,
            has_more=has_more,
        )
    except Exception as e:
        logger.error(f"Error previewing propagation for family {family_id}: {e}")
        raise


async def apply_family_propagation(
    *,
    family_id: str,
    expected_base_version: int,
    expected_current_versions: Dict[str, int],
    resolutions: List[PropagationResolution],
    changed_by: Optional[str],
) -> BulkUpdateResponse:
    """Write the previewed merge into every child, all-or-nothing.

    Re-runs the three-way merge from scratch inside one transaction with
    ``FOR UPDATE`` locks on the family and all members — the preview is
    stateless, so the merge that gets applied is recomputed from the
    just-locked rows and the client's expectations are only assertions
    (docs/TEMPLATE_LINEAGE.md §6.3 step 4).
    """
    resolution_index: Dict[Tuple[str, str], PropagationResolution] = {
        (r.template_id, r.field_path): r for r in resolutions
    }
    used: Set[Tuple[str, str]] = set()
    async for conn in get_db_connection():
        async with conn.transaction():
            fam_q, fam_v = get_template_family_for_update_query(family_id)
            family = await conn.fetchrow(fam_q, *fam_v)
            if family is None:
                return BulkUpdateResponse(status="failed", results=[])
            to_base_version = family["base_version"]
            if to_base_version != expected_base_version:
                return BulkUpdateResponse(
                    status="drift",
                    results=[
                        BulkTemplateResult(
                            template_id="",
                            name="",
                            error=(
                                f"family base_version is {to_base_version}, the "
                                f"preview was computed at {expected_base_version}; "
                                "re-run propagate/preview"
                            ),
                        )
                    ],
                )
            previous_base_version = to_base_version - 1 if to_base_version > 1 else None
            parent_flow = _json_col(family, "flow") or {}
            parent_configurations = _json_col(family, "configurations")

            members_q, members_v = get_family_templates_for_update_query(family_id)
            members = await conn.fetch(members_q, *members_v)
            if not members:
                return BulkUpdateResponse(status="completed", results=[])

            drifted = [
                BulkTemplateResult(
                    template_id=str(row["id"]),
                    name=row["name"],
                    from_version=row["current_version"],
                    error=(
                        # Preview is paginated; apply is not. An uncovered
                        # member means the caller applied without loading
                        # every preview page, or the member joined the
                        # family after the preview was built.
                        "not covered by this preview — load every preview "
                        "page before applying (or the member joined the "
                        "family after the preview was built)"
                        if str(row["id"]) not in expected_current_versions
                        else (
                            f"drifted: head is v{row['current_version']}, preview "
                            f"saw v{expected_current_versions[str(row['id'])]}"
                        )
                    ),
                )
                for row in members
                if expected_current_versions.get(str(row["id"]))
                != row["current_version"]
            ]
            if drifted:
                return BulkUpdateResponse(status="drift", results=drifted)

            async def fetch_snapshot(fid: str, base_version: int):
                q, v = get_family_version_snapshot_query(fid, base_version)
                return await conn.fetchrow(q, *v)

            planned: List[
                Tuple[Any, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]
            ] = []
            unresolved: List[BulkTemplateResult] = []
            errors: List[BulkTemplateResult] = []
            for row in members:
                template_id = str(row["id"])
                base_flow, base_configurations, _base_version, _source = (
                    await _resolve_merge_base(
                        fetch_snapshot,
                        family_id,
                        row.get("derived_from_base_version"),
                        previous_base_version,
                        parent_flow,
                        parent_configurations,
                    )
                )
                child_flow = _json_col(row, "flow") or {}
                child_configurations = _json_col(row, "configurations")
                try:
                    outcome = merge_family_into_child(
                        base_flow=base_flow,
                        base_configurations=base_configurations,
                        parent_flow=parent_flow,
                        parent_configurations=parent_configurations,
                        child_flow=child_flow,
                        child_configurations=child_configurations,
                    )
                except ValueError as exc:
                    errors.append(
                        BulkTemplateResult(
                            template_id=template_id,
                            name=row["name"],
                            from_version=row["current_version"],
                            error=str(exc),
                        )
                    )
                    continue

                changes: List[MergeFieldChange] = list(outcome.auto_apply)
                missing: List[str] = []
                for conflict in outcome.conflicts:
                    key = (template_id, conflict.field_path)
                    resolution = resolution_index.get(key)
                    if resolution is None:
                        missing.append(conflict.field_path)
                        continue
                    used.add(key)
                    if resolution.choice == "child":
                        continue
                    if resolution.choice == "parent":
                        take_op = "set" if conflict.parent_present else "remove"
                        take_value = (
                            conflict.parent if conflict.parent_present else None
                        )
                    else:
                        take_op = "set"
                        take_value = resolution.value
                    if (
                        take_op == "set"
                        and conflict.field_path.startswith("configurations.")
                        and contains_masked_literal(take_value)
                    ):
                        # Two ways a masked literal reaches this point:
                        # "custom" resolutions built from the preview (which
                        # masks configurations.* conflict values), and "parent"
                        # resolutions, since family configurations are
                        # mask-level by construction -- mask_mcp_auth_secrets
                        # runs on every family write. Never persist it: that
                        # would overwrite the child's real MCP auth secret
                        # with the literal "**********" and silently break
                        # auth at runtime. Treat it as "unchanged" instead,
                        # mirroring merge_masked_mcp_auth's semantics --
                        # substitute the child's current raw value at this
                        # field path, so the non-secret part of the same
                        # parent edit still applies.
                        if not conflict.child_present:
                            missing.append(
                                f"{conflict.field_path} (resolved value "
                                "contains a masked secret placeholder with "
                                "no existing child value to restore -- "
                                "resubmit as choice='custom' with the real "
                                "value)"
                            )
                            continue
                        take_value = conflict.child
                    changes.append(
                        MergeFieldChange(
                            field_path=conflict.field_path,
                            op=take_op,
                            value=take_value,
                        )
                    )
                if missing:
                    unresolved.append(
                        BulkTemplateResult(
                            template_id=template_id,
                            name=row["name"],
                            from_version=row["current_version"],
                            error="unresolved conflicts: " + ", ".join(sorted(missing)),
                        )
                    )
                    continue
                if not changes:
                    # Nothing to write (no changes, or every conflict resolved
                    # "keep child") — still marked as synced below.
                    planned.append((row, None, None))
                    continue
                try:
                    new_flow, new_configurations = apply_merge_decisions(
                        child_flow, child_configurations, changes
                    )
                except ValueError as exc:
                    errors.append(
                        BulkTemplateResult(
                            template_id=template_id,
                            name=row["name"],
                            from_version=row["current_version"],
                            error=str(exc),
                        )
                    )
                    continue
                err = validate_patched_template(
                    new_flow, new_configurations, template_id, row["name"]
                )
                if err:
                    errors.append(
                        BulkTemplateResult(
                            template_id=template_id,
                            name=row["name"],
                            from_version=row["current_version"],
                            error=err,
                        )
                    )
                    continue
                planned.append((row, new_flow, new_configurations))

            if unresolved:
                return BulkUpdateResponse(status="unresolved", results=unresolved)
            if errors:
                return BulkUpdateResponse(status="failed", results=errors)
            stale = sorted(
                f"{tid}:{path}" for tid, path in set(resolution_index) - used
            )
            if stale:
                # A resolution the merge never produced means the client is
                # working from a stale preview; applying anyway would write a
                # different merge than the human approved.
                return BulkUpdateResponse(
                    status="failed",
                    results=[
                        BulkTemplateResult(
                            template_id="",
                            name="",
                            error=(
                                "resolutions match no conflict (stale preview): "
                                + ", ".join(stale)
                            ),
                        )
                    ],
                )

            op_id = str(uuid4())
            patch_json = json.dumps(
                {
                    "propagation": {
                        "family_id": family_id,
                        "from_base_version": previous_base_version,
                        "to_base_version": to_base_version,
                        "resolutions": [r.model_dump() for r in resolutions],
                    }
                }
            )
            op_q, op_v = insert_bulk_op_query(
                op_id,
                "propagation",
                family_id,
                # Every child the op COVERED, not just the ones whose content
                # changed: the sync pointer below is advanced for all of them,
                # so a rollback has to reset all of them too (content restore
                # stays driven by the touched version rows, which only the
                # written children have).
                [str(row["id"]) for row, _, _ in planned],
                patch_json,
                "completed",
                None,
                changed_by,
                previous_base_version,
                to_base_version,
            )
            op_row = await conn.fetchrow(op_q, *op_v)

            results: List[BulkTemplateResult] = []
            now = datetime.now(timezone.utc)
            for row, new_flow, new_configurations in planned:
                template_id = str(row["id"])
                to_version = row["current_version"]
                if new_flow is not None:
                    upd_q, upd_v = update_template_bulk_fields_query(
                        template_id,
                        json.dumps(new_flow),
                        (
                            json.dumps(new_configurations)
                            if new_configurations is not None
                            else None
                        ),
                        now,
                    )
                    head = await conn.fetchrow(upd_q, *upd_v)
                    await insert_template_version_on_conn(
                        conn,
                        head,
                        change_source="bulk_update",
                        bulk_op_id=op_id,
                        changed_by=changed_by,
                    )
                    to_version = head["current_version"]
                sync_q, sync_v = set_template_derived_base_version_query(
                    template_id, to_base_version
                )
                await conn.execute(sync_q, *sync_v)
                results.append(
                    BulkTemplateResult(
                        template_id=template_id,
                        name=row["name"],
                        from_version=row["current_version"],
                        to_version=to_version,
                    )
                )
            return BulkUpdateResponse(
                status="completed", bulk_op_id=str(op_row["id"]), results=results
            )
    return BulkUpdateResponse(status="failed", results=[])
