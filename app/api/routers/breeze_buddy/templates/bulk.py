"""Admin-only routes: family assignment, bulk update/rollback, op ledger."""

from fastapi import APIRouter, Depends, Query

from app.api.security.breeze_buddy.rbac_token import get_current_user_with_rbac
from app.core.security.authorization import require_admin
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

from .bulk_handlers import (
    bulk_rollback_handler,
    bulk_update_handler,
    create_family_handler,
    get_family_handler,
    get_family_version_handler,
    list_bulk_ops_handler,
    list_families_handler,
    list_family_versions_handler,
    propagate_apply_handler,
    propagate_preview_handler,
    rollback_family_handler,
    update_family_handler,
    update_family_members_handler,
)

router = APIRouter()


@router.post("/templates/families", response_model=FamilyResponse, status_code=201)
async def create_family(
    body: CreateFamilyRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Create a family: name + base (parent) template + initial members. Admin only."""
    require_admin(current_user)
    return await create_family_handler(body, current_user)


@router.get("/templates/families", response_model=FamilyListResponse)
async def list_families(
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """List all global families (admin-managed). Admin only."""
    require_admin(current_user)
    return await list_families_handler(current_user)


@router.get("/templates/families/{family_id}", response_model=FamilyResponse)
async def get_family(
    family_id: str,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """One family: parent template content inline + all members with
    versions (global admin-managed). Admin only."""
    require_admin(current_user)
    return await get_family_handler(family_id, current_user)


@router.put("/templates/families/{family_id}", response_model=FamilyResponse)
async def update_family(
    family_id: str,
    body: UpdateFamilyRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Edit the family's parent template content (bumps base_version). Admin only."""
    require_admin(current_user)
    return await update_family_handler(family_id, body, current_user)


@router.patch("/templates/families/{family_id}/members", response_model=FamilyResponse)
async def update_family_members(
    family_id: str,
    body: UpdateFamilyMembersRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Add/remove member templates. Admin only."""
    require_admin(current_user)
    return await update_family_members_handler(family_id, body, current_user)


@router.post("/templates/bulk/update", response_model=BulkUpdateResponse)
async def bulk_update(
    body: BulkUpdateRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Apply a JSON merge patch to every template in a family (or explicit id
    list). All-or-nothing; use dry_run=true to preview. Admin only."""
    require_admin(current_user)
    return await bulk_update_handler(body, current_user)


@router.post("/templates/bulk/rollback", response_model=BulkRollbackResponse)
async def bulk_rollback(
    body: BulkRollbackRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Revert a completed bulk update. Refuses if any member was edited
    afterwards, unless force=true. Admin only."""
    require_admin(current_user)
    return await bulk_rollback_handler(body, current_user)


@router.get("/templates/bulk/ops", response_model=BulkOpListResponse)
async def list_bulk_ops(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Ledger of bulk operations (admin only) — source of rollback targets."""
    require_admin(current_user)
    return await list_bulk_ops_handler(limit, offset)


@router.get(
    "/templates/families/{family_id}/versions",
    response_model=FamilyVersionListResponse,
)
async def list_family_versions(
    family_id: str,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """History of the family's parent template (newest base_version first). Admin only."""
    require_admin(current_user)
    return await list_family_versions_handler(family_id, limit, offset, current_user)


@router.get(
    "/templates/families/{family_id}/versions/{base_version}",
    response_model=FamilyVersionDetailResponse,
)
async def get_family_version(
    family_id: str,
    base_version: int,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """One historical revision of the family's parent template. Admin only."""
    require_admin(current_user)
    return await get_family_version_handler(family_id, base_version, current_user)


@router.post("/templates/families/{family_id}/rollback", response_model=FamilyResponse)
async def rollback_family(
    family_id: str,
    body: RollbackFamilyRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Restore an older parent-template revision as a NEW base_version.
    Children are untouched — run propagate/preview afterwards to carry the
    restore into them. Admin only."""
    require_admin(current_user)
    return await rollback_family_handler(family_id, body, current_user)


@router.post(
    "/templates/families/{family_id}/propagate/preview",
    response_model=PropagationPreviewResponse,
)
async def propagate_preview(
    family_id: str,
    page: int = Query(1, ge=1, description="1-based page number"),
    limit: int = Query(50, ge=1, le=200, description="Children per page"),
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Three-way merge the family's current parent template into one page of
    children: per-child auto-applies, no-ops and conflicts. Writes nothing.
    Paginate with ``page`` / ``limit`` for large families. Admin only."""
    require_admin(current_user)
    return await propagate_preview_handler(family_id, page, limit, current_user)


@router.post(
    "/templates/families/{family_id}/propagate/apply",
    response_model=BulkUpdateResponse,
)
async def propagate_apply(
    family_id: str,
    body: PropagateApplyRequest,
    current_user: UserInfo = Depends(get_current_user_with_rbac),
):
    """Apply the previewed merge with the lead's conflict resolutions: one
    transaction, one bulk_op_id, a version snapshot per changed child.
    409 if anything moved since the preview, 422 if conflicts are
    unresolved. Reversible via POST /templates/bulk/rollback. Admin only."""
    require_admin(current_user)
    return await propagate_apply_handler(family_id, body, current_user)
