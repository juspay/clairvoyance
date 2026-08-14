"""Handlers for family assignment and bulk template operations (admin only)."""

import asyncio
import json
from typing import NoReturn

import asyncpg
from fastapi import HTTPException, status

from app.ai.voice.agents.breeze_buddy.template.cache import invalidate_template
from app.ai.voice.agents.breeze_buddy.template.patch import validate_patched_template
from app.core.logger import logger
from app.database.accessor.breeze_buddy.template import (
    get_template_by_id,
    get_template_raw_configurations,
)
from app.database.accessor.breeze_buddy.template_version import (
    PreOpSnapshotVanished,
    apply_family_propagation,
    bulk_rollback_templates,
    bulk_update_templates,
    create_template_family,
    get_bulk_op,
    get_family_version,
    get_template_family,
    list_bulk_ops,
    list_family_versions,
    list_template_families,
    preview_family_propagation,
    rollback_family_to_version,
    update_family_members,
    update_template_family,
)
from app.schemas import UserInfo
from app.schemas.breeze_buddy.template_version import (
    BulkOpListResponse,
    BulkRollbackRequest,
    BulkRollbackResponse,
    BulkUpdateRequest,
    BulkUpdateResponse,
    CreateFamilyRequest,
    FamilyListResponse,
    FamilyResponse,
    FamilyVersionDetailResponse,
    FamilyVersionListResponse,
    PropagateApplyRequest,
    PropagationPreviewResponse,
    RollbackFamilyRequest,
    UpdateFamilyMembersRequest,
    UpdateFamilyRequest,
)


def _abort(status_code: int, message: str, results) -> NoReturn:
    raise HTTPException(
        status_code=status_code,
        detail={"message": message, "results": [r.model_dump() for r in results]},
    )


async def _invalidate_all(template_ids):
    """Best-effort, concurrent: a bulk op can touch hundreds of templates and
    the caller is already holding the HTTP response open. One failed Redis
    round trip must not abort the others (the cache is a soft dependency —
    a stale entry expires on its own TTL)."""
    ids = list(template_ids)
    if not ids:
        return
    outcomes = await asyncio.gather(
        *(invalidate_template(tid) for tid in ids), return_exceptions=True
    )
    for tid, outcome in zip(ids, outcomes):
        if isinstance(outcome, BaseException):
            logger.warning(f"cache invalidation failed for {tid}: {outcome}")


async def create_family_handler(
    body: CreateFamilyRequest, current_user: UserInfo
) -> FamilyResponse:
    # Resolve the parent template content: inline fields, or copied from an
    # existing template (content columns only — never secrets/routing).
    flow = body.flow
    eps = body.expected_payload_schema
    ecrs = body.expected_callback_response_schema
    configurations = body.configurations
    channels = body.supported_channels
    if body.copy_base_from_template_id:
        source = await get_template_by_id(body.copy_base_from_template_id)
        if not source:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Source template not found: {body.copy_base_from_template_id}",
            )
        flow = flow or source.flow
        eps = eps or source.expected_payload_schema
        ecrs = ecrs or source.expected_callback_response_schema
        if configurations is None and source.configurations:
            # The RAW column, not source.configurations.model_dump(): the
            # decoded model fills in every Pydantic default, which would
            # copy ~10 keys the source template never set into the family
            # parent (and into every snapshot of it).
            #
            # Secrets are handled a layer down: create_template_family runs
            # mask_mcp_auth_secrets over this, so any MCP auth token becomes
            # an inert placeholder. The family parent is reference-only
            # content (never rendered into a live call), and a member
            # template's own secrets override it at call time.
            configurations = await get_template_raw_configurations(
                body.copy_base_from_template_id
            )
        channels = channels or list(source.supported_channels)
    validation_error = validate_patched_template(
        flow or {}, configurations, template_id="family", template_name=body.name
    )
    if validation_error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=validation_error
        )
    try:
        family = await create_template_family(
            name=body.name,
            description=body.description,
            flow=flow or {},
            expected_payload_schema=eps,
            expected_callback_response_schema=ecrs,
            configurations=configurations,
            supported_channels=channels or ["voice"],
            member_template_ids=body.member_template_ids,
            created_by=current_user.username,
        )
    except asyncpg.UniqueViolationError:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A family named '{body.name}' already exists",
        )
    except ValueError as e:
        # Member list rejected (missing template, or cross-reseller): the
        # family was not created either.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    if family is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create family",
        )
    if body.member_template_ids:
        # assign_family (called by create_template_family) sets
        # template.family_id but never busts the per-template cache;
        # best-effort invalidate so a cached read doesn't serve a stale
        # family_id right after assignment.
        await _invalidate_all(body.member_template_ids)
    logger.info(
        f"Admin {current_user.username} created family {family.id} "
        f"('{body.name}', {len(body.member_template_ids)} members)"
    )
    return family


async def _load_family_or_404(family_id: str) -> FamilyResponse:
    family = await get_template_family(family_id)
    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Family not found: {family_id}",
        )
    return family


async def update_family_handler(
    family_id: str, body: UpdateFamilyRequest, current_user: UserInfo
) -> FamilyResponse:
    existing = await _load_family_or_404(family_id)
    if body.flow is not None or body.configurations is not None:
        # Validation only: `existing` is the decoded (default-filled) view,
        # which is a superset of what is stored and therefore safe to
        # validate against. What gets PERSISTED for an unsent field is the
        # raw stored column — update_template_family carries it over.
        validation_error = validate_patched_template(
            (body.flow if body.flow is not None else existing.flow) or {},
            (
                body.configurations
                if body.configurations is not None
                else existing.configurations
            ),
            template_id="family",
            template_name=(body.name if body.name is not None else existing.name),
        )
        if validation_error:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=validation_error
            )
    # Unset fields are passed through as None ("leave this column alone")
    # rather than echoed back from `existing`.
    family = await update_template_family(
        family_id,
        name=body.name,
        description=body.description,
        flow=body.flow,
        expected_payload_schema=body.expected_payload_schema,
        expected_callback_response_schema=body.expected_callback_response_schema,
        configurations=body.configurations,
        supported_channels=body.supported_channels,
        updated_by=current_user.username,
    )
    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Family not found: {family_id}",
        )
    logger.info(
        f"Admin {current_user.username} updated family {family_id} "
        f"(base_version -> {family.base_version})"
    )
    return family


async def get_family_handler(family_id: str, current_user: UserInfo) -> FamilyResponse:
    family = await _load_family_or_404(family_id)
    return family


async def list_families_handler(current_user: UserInfo) -> FamilyListResponse:
    return FamilyListResponse(families=await list_template_families())


async def update_family_members_handler(
    family_id: str, body: UpdateFamilyMembersRequest, current_user: UserInfo
) -> FamilyResponse:
    try:
        family = await update_family_members(
            family_id, add=body.add_template_ids, remove=body.remove_template_ids
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)
        ) from e
    if family is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Family not found: {family_id}",
        )
    # assign_family / the remove path change template.family_id but never
    # bust the per-template cache; best-effort invalidate every added and
    # removed member so a cached read doesn't serve a stale family_id.
    changed_member_ids = list(body.add_template_ids) + list(body.remove_template_ids)
    if changed_member_ids:
        await _invalidate_all(changed_member_ids)
    logger.info(
        f"Admin {current_user.username} updated members of family {family_id} "
        f"(+{len(body.add_template_ids)}/-{len(body.remove_template_ids)})"
    )
    return family


async def bulk_update_handler(
    body: BulkUpdateRequest, current_user: UserInfo
) -> BulkUpdateResponse:
    patch_json = json.dumps(
        {
            "flow_patch": body.flow_patch,
            "node_patches": body.node_patches,
            "configurations_patch": body.configurations_patch,
        }
    )
    resp = await bulk_update_templates(
        template_ids=body.template_ids,
        family_id=body.family_id,
        flow_patch=body.flow_patch,
        node_patches=body.node_patches,
        configurations_patch=body.configurations_patch,
        patch_json=patch_json,
        changed_by=current_user.username,
        dry_run=body.dry_run,
    )
    if resp.status == "completed":
        await _invalidate_all([r.template_id for r in resp.results])
        logger.info(
            f"Bulk update {resp.bulk_op_id} by {current_user.username}: "
            f"{len(resp.results)} templates updated"
        )
    elif resp.status == "failed":
        # 422 with the full per-template error report
        _abort(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Bulk update aborted; no template was modified",
            resp.results,
        )
    return resp


async def list_bulk_ops_handler(limit: int, offset: int) -> BulkOpListResponse:
    return BulkOpListResponse(ops=await list_bulk_ops(limit, offset))


async def bulk_rollback_handler(
    body: BulkRollbackRequest, current_user: UserInfo
) -> BulkRollbackResponse:
    op = await get_bulk_op(body.bulk_op_id)
    if op is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Bulk op not found: {body.bulk_op_id}",
        )
    if op.op_type not in ("bulk_update", "propagation") or op.status != "completed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Bulk op {body.bulk_op_id} is {op.op_type}/{op.status}; only "
                "completed bulk_update or propagation ops can be rolled back"
            ),
        )
    if body.also_revert_family and op.op_type != "propagation":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "also_revert_family is only valid for propagation ops; "
                f"{body.bulk_op_id} is {op.op_type}"
            ),
        )
    if body.also_revert_family and op.from_base_version is None:
        # force can't help here: there is no prior family revision to
        # restore, so the generic "retry with force=true" 409 below would
        # be misleading.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "family has no prior version to restore (this propagation "
                "created base_version 1); re-run without also_revert_family"
            ),
        )
    try:
        resp = await bulk_rollback_templates(
            body.bulk_op_id,
            changed_by=current_user.username,
            force=body.force,
            also_revert_family=body.also_revert_family,
            op=op,
        )
    except PreOpSnapshotVanished as e:
        # A member was deleted mid-rollback; the transaction rolled back, so
        # nothing was reverted. Same 409 shape as the pruned-snapshot abort.
        logger.opt(exception=e).warning(
            f"Bulk rollback of {body.bulk_op_id} aborted: template "
            f"{e.template_id} vanished mid-transaction"
        )
        _abort(
            status.HTTP_409_CONFLICT,
            (
                "Bulk rollback aborted: pre-op snapshot missing for template "
                f"{e.template_id} (deleted while the rollback ran); nothing "
                "was reverted"
            ),
            [],
        )
    if resp.status == "failed":
        # Distinct all-or-nothing abort reasons share status="failed":
        # drifted heads (retryable with force=true), pruned pre-op snapshots,
        # and (propagation-only) a pruned family snapshot -- none of the
        # latter two are retryable, so force does not override them.
        # Distinguish by inspecting the error tag each accessor path stamps
        # onto its per-template results.
        family_snapshot_missing = any(
            r.error and "family snapshot missing" in r.error for r in resp.results
        )
        if family_snapshot_missing:
            _abort(
                status.HTTP_409_CONFLICT,
                (
                    "Bulk rollback aborted: the family revision this "
                    "propagation started from was pruned by retention; "
                    "nothing was reverted"
                ),
                resp.results,
            )
        missing_snapshots = any(
            r.error and "pre-op snapshot missing" in r.error for r in resp.results
        )
        if missing_snapshots:
            _abort(
                status.HTTP_409_CONFLICT,
                (
                    "Bulk rollback aborted: pre-op snapshot missing (pruned "
                    "by retention) for one or more templates; nothing was "
                    "reverted"
                ),
                resp.results,
            )
        _abort(
            status.HTTP_409_CONFLICT,
            (
                "Bulk rollback aborted (templates edited after the bulk op); "
                "retry with force=true to override"
            ),
            resp.results,
        )
    await _invalidate_all([r.template_id for r in resp.results])
    logger.info(
        f"Bulk rollback {resp.bulk_op_id} by {current_user.username} "
        f"reverted op {body.bulk_op_id} across {len(resp.results)} templates"
    )
    return resp


async def list_family_versions_handler(
    family_id: str, limit: int, offset: int, current_user: UserInfo
) -> FamilyVersionListResponse:
    family = await _load_family_or_404(family_id)
    versions, total = await list_family_versions(family_id, limit, offset)
    return FamilyVersionListResponse(
        family_id=family_id,
        base_version=family.base_version,
        versions=versions,
        total=total,
    )


async def get_family_version_handler(
    family_id: str, base_version: int, current_user: UserInfo
) -> FamilyVersionDetailResponse:
    await _load_family_or_404(family_id)
    found = await get_family_version(family_id, base_version)
    if found is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {base_version} not found for family {family_id}",
        )
    snapshot, meta = found
    return FamilyVersionDetailResponse(meta=meta, snapshot=snapshot)


async def rollback_family_handler(
    family_id: str, body: RollbackFamilyRequest, current_user: UserInfo
) -> FamilyResponse:
    await _load_family_or_404(family_id)
    restored = await rollback_family_to_version(
        family_id, body.base_version, current_user.username
    )
    if restored is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Version {body.base_version} not found for family {family_id}",
        )
    logger.info(
        f"Admin {current_user.username} rolled family {family_id} back to "
        f"base_version {body.base_version} (new base_version "
        f"{restored.base_version})"
    )
    # The parent template is reference-only content — no member template
    # changed, so there is no per-template cache to invalidate here.
    return restored


async def propagate_preview_handler(
    family_id: str, page: int, limit: int, current_user: UserInfo
) -> PropagationPreviewResponse:
    preview = await preview_family_propagation(family_id, page=page, limit=limit)
    if preview is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Family not found: {family_id}",
        )
    logger.info(
        f"Admin {current_user.username} previewed propagation of family "
        f"{family_id} to_base_version {preview.to_base_version}: "
        f"{len(preview.children)} children (page {page}/{limit}), {preview.conflict_count} conflicts"
    )
    return preview


async def propagate_apply_handler(
    family_id: str, body: PropagateApplyRequest, current_user: UserInfo
) -> BulkUpdateResponse:
    await _load_family_or_404(family_id)
    resp = await apply_family_propagation(
        family_id=family_id,
        expected_base_version=body.expected_base_version,
        expected_current_versions=body.expected_current_versions,
        resolutions=body.resolutions,
        changed_by=current_user.username,
    )
    if resp.status == "drift":
        _abort(
            status.HTTP_409_CONFLICT,
            (
                "Propagation aborted: the family or a child changed since "
                "the preview; nothing was written. Re-run propagate/preview"
            ),
            resp.results,
        )
    if resp.status in ("unresolved", "failed"):
        _abort(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            (
                "Propagation aborted: unresolved conflicts"
                if resp.status == "unresolved"
                else "Propagation aborted; no template was modified"
            ),
            resp.results,
        )
    await _invalidate_all([r.template_id for r in resp.results])
    logger.info(
        f"Propagation {resp.bulk_op_id} by {current_user.username} on family "
        f"{family_id}: {len(resp.results)} children"
    )
    return resp
