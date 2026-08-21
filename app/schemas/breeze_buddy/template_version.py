"""Schemas for template versioning (lineage) endpoints."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel


class TemplateVersionMetadata(BaseModel):
    template_id: str
    version: int
    change_source: str
    bulk_op_id: Optional[str] = None
    changed_by: Optional[str] = None
    created_at: datetime


class TemplateVersionListResponse(BaseModel):
    template_id: str
    current_version: int
    versions: List[TemplateVersionMetadata]
    total: int


class TemplateVersionDetailResponse(BaseModel):
    meta: TemplateVersionMetadata
    snapshot: TemplateModel


class RollbackTemplateRequest(BaseModel):
    version: int = Field(ge=1)


class CreateFamilyRequest(BaseModel):
    name: str
    description: Optional[str] = None
    # Parent template content, inline (mirrors template's content columns)...
    flow: Optional[Dict[str, Any]] = None
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[Dict[str, Any]] = None
    supported_channels: Optional[List[str]] = None
    # ...or copied from an existing template (content columns only —
    # secrets / routing columns are never copied into a family).
    copy_base_from_template_id: Optional[str] = None
    member_template_ids: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_base_content(self) -> "CreateFamilyRequest":
        if not self.flow and not self.copy_base_from_template_id:
            raise ValueError(
                "provide flow (parent template content) or copy_base_from_template_id"
            )
        return self


class UpdateFamilyRequest(BaseModel):
    """PUT body: edit the family's parent template content (bumps base_version).
    Omitted fields keep their persisted values."""

    name: Optional[str] = None
    description: Optional[str] = None
    flow: Optional[Dict[str, Any]] = None
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[Dict[str, Any]] = None
    supported_channels: Optional[List[str]] = None


class FamilyMember(BaseModel):
    template_id: str
    name: str
    reseller_id: str
    merchant_id: Optional[str] = None
    current_version: int
    # NULL until the child has been through a propagation; the family
    # screen renders it as "never synced".
    derived_from_base_version: Optional[int] = None


class FamilyResponse(BaseModel):
    id: str
    name: str
    description: Optional[str] = None
    # the embedded parent template
    flow: Dict[str, Any] = Field(default_factory=dict)
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[Dict[str, Any]] = None
    supported_channels: List[str] = Field(default_factory=lambda: ["voice"])
    base_version: int = 1
    # The listing endpoint fills member_count and leaves members empty (one
    # aggregate query instead of one member query per family); the detail
    # endpoint returns both.
    members: List[FamilyMember] = Field(default_factory=list)
    member_count: int = 0
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


class FamilyListResponse(BaseModel):
    families: List[FamilyResponse]


class UpdateFamilyMembersRequest(BaseModel):
    add_template_ids: List[str] = Field(default_factory=list)
    remove_template_ids: List[str] = Field(default_factory=list)


class BulkUpdateRequest(BaseModel):
    family_id: Optional[str] = None
    template_ids: Optional[List[str]] = Field(default=None, min_length=1)
    flow_patch: Optional[Dict[str, Any]] = None
    node_patches: Optional[Dict[str, Dict[str, Any]]] = None
    configurations_patch: Optional[Dict[str, Any]] = None
    dry_run: bool = False

    @model_validator(mode="after")
    def _validate_scope_and_patch(self) -> "BulkUpdateRequest":
        if bool(self.family_id) == bool(self.template_ids):
            raise ValueError("provide exactly one of family_id or template_ids")
        if not (self.flow_patch or self.node_patches or self.configurations_patch):
            raise ValueError(
                "at least one of flow_patch, node_patches, configurations_patch is required"
            )
        # A merge patch replaces arrays wholesale — 'nodes' in flow_patch would
        # silently overwrite every member's full node list. Node edits are only
        # allowed through node_patches (addressed by node_name).
        if self.flow_patch and "nodes" in self.flow_patch:
            raise ValueError(
                "flow_patch must not contain 'nodes'; use node_patches to edit nodes"
            )
        return self


class BulkTemplateResult(BaseModel):
    template_id: str
    name: str
    from_version: Optional[int] = None
    to_version: Optional[int] = None
    error: Optional[str] = None
    # dry_run only: this member's patched content, so callers can preview the
    # effect of the patch without applying it. None on real (non-dry_run)
    # results — the patch is already persisted, so a preview is redundant.
    preview_flow: Optional[Dict[str, Any]] = None
    preview_configurations: Optional[Dict[str, Any]] = None


class BulkUpdateResponse(BaseModel):
    status: str  # "completed" | "dry_run" | "failed"
    bulk_op_id: Optional[str] = None
    results: List[BulkTemplateResult]


class BulkRollbackRequest(BaseModel):
    bulk_op_id: str
    force: bool = False  # override the drifted-template guard
    # propagation ops only: also restore the family's parent template to the
    # revision the propagation started from (as a NEW family base_version,
    # same transaction as the children's revert).
    also_revert_family: bool = False


class BulkRollbackResponse(BaseModel):
    status: str
    bulk_op_id: str  # the new bulk_rollback op
    reverted_bulk_op_id: str
    results: List[BulkTemplateResult]
    # set when also_revert_family reverted the family content too
    family_reverted_to_base_version: Optional[int] = None


class BulkOpSummary(BaseModel):
    id: str
    op_type: str
    family_id: Optional[str] = None
    template_ids: List[str]
    status: str
    reverted_bulk_op_id: Optional[str] = None
    initiated_by: Optional[str] = None
    created_at: datetime
    # propagation ops only: which family revision children moved from / to
    from_base_version: Optional[int] = None
    to_base_version: Optional[int] = None
    # Computed (list_bulk_ops only): for completed bulk_update ops, how many
    # touched templates can no longer be rolled back because their pre-op
    # snapshot was pruned by retention. None for bulk_rollback / non-completed
    # ops, where the concept doesn't apply.
    rollback_unavailable_count: Optional[int] = None


class BulkOpListResponse(BaseModel):
    ops: List[BulkOpSummary]


class MergeFieldChange(BaseModel):
    """One field the propagation will write into a child.

    ``op="remove"`` deletes the field/node (the family edit removed it);
    ``value`` is then always None.
    """

    field_path: str
    op: Literal["set", "remove"] = "set"
    value: Any = None


class MergeFieldConflict(BaseModel):
    """One field the merchant customized where the family also changed it.

    The ``*_present`` flags disambiguate "the value is JSON null" from "the
    field does not exist on that side" — the difference between setting a
    key to null and deleting it.
    """

    field_path: str
    base: Any = None
    parent: Any = None
    child: Any = None
    base_present: bool = True
    parent_present: bool = True
    child_present: bool = True


class ChildMergeOutcome(BaseModel):
    auto_apply: List[MergeFieldChange] = Field(default_factory=list)
    noop: List[str] = Field(default_factory=list)
    conflicts: List[MergeFieldConflict] = Field(default_factory=list)


class FamilyVersionMetadata(BaseModel):
    family_id: str
    base_version: int
    name: str
    changed_by: Optional[str] = None
    created_at: datetime


class FamilyVersionSnapshot(BaseModel):
    """The family's parent-template content at one base_version."""

    name: str
    description: Optional[str] = None
    flow: Dict[str, Any] = Field(default_factory=dict)
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    configurations: Optional[Dict[str, Any]] = None
    supported_channels: List[str] = Field(default_factory=lambda: ["voice"])


class FamilyVersionListResponse(BaseModel):
    family_id: str
    base_version: int  # the family's current (head) base_version
    versions: List[FamilyVersionMetadata]
    total: int


class FamilyVersionDetailResponse(BaseModel):
    meta: FamilyVersionMetadata
    snapshot: FamilyVersionSnapshot


class RollbackFamilyRequest(BaseModel):
    base_version: int = Field(ge=1)


class PropagationChildPreview(BaseModel):
    template_id: str
    name: str
    merchant_id: Optional[str] = None
    current_version: int
    derived_from_base_version: Optional[int] = None
    # Which family version the merge actually used as its base, and why.
    merge_base_version: Optional[int] = None
    base_source: str  # "derived" | "previous_version" | "parent_fallback"
    auto_applied: List[MergeFieldChange] = Field(default_factory=list)
    noop: List[str] = Field(default_factory=list)
    conflicts: List[MergeFieldConflict] = Field(default_factory=list)
    # Set when this child could not be merged at all (e.g. an unaddressable
    # key); the rest of the preview still renders.
    error: Optional[str] = None


class PropagationPreviewResponse(BaseModel):
    """Stateless preview: no token, no server-side state. The UI posts
    ``to_base_version`` and ``expected_current_versions`` back to apply,
    which re-runs this exact merge and uses them as concurrency assertions.

    Pagination: ``total_members`` / ``page`` / ``limit`` / ``has_more``
    support large families. The UI fetches one page at a time, accumulates
    the per-child data, and defers apply until all pages are loaded and every
    conflict on every page is resolved.
    """

    family_id: str
    to_base_version: int  # the family revision being propagated
    from_base_version: Optional[int] = None
    children: List[PropagationChildPreview] = Field(default_factory=list)
    conflict_count: int = 0
    expected_current_versions: Dict[str, int] = Field(default_factory=dict)
    # Pagination metadata
    total_members: int = 0
    page: int = 1
    limit: int = 50
    has_more: bool = False


class PropagationResolution(BaseModel):
    """One human decision on one conflict from the preview.

    ``parent`` takes the family's new value (deleting the field when the
    family edit deleted it), ``child`` keeps the merchant's value and writes
    nothing, ``custom`` sets ``value`` verbatim.
    """

    template_id: str
    field_path: str
    choice: Literal["parent", "child", "custom"]
    value: Any = None

    @model_validator(mode="after")
    def _custom_requires_value(self) -> "PropagationResolution":
        if self.choice == "custom" and self.value is None:
            raise ValueError(
                "choice='custom' requires a non-null value; to delete a field "
                "resolve the removal conflict with choice='parent'"
            )
        return self


class PropagateApplyRequest(BaseModel):
    """Optimistic concurrency: both expectations come straight from the
    preview response. Apply re-runs the merge and refuses (409) if either
    moved, because the human approved a merge computed from that state."""

    expected_base_version: int = Field(ge=1)
    expected_current_versions: Dict[str, int]
    resolutions: List[PropagationResolution] = Field(default_factory=list)
