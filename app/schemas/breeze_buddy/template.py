"""Response schemas for template endpoints."""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class TemplateMetadata(BaseModel):
    """Lightweight template metadata without flow structure.

    Used for listing templates where the full flow is not needed.
    This reduces response size by ~98.5% compared to full template objects.
    """

    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    is_active: bool
    supported_channels: List[str] = ["voice"]
    created_at: datetime
    updated_at: datetime


class TemplateListResponse(BaseModel):
    """Response for listing templates.

    Returns metadata for all accessible templates based on user's RBAC permissions.
    """

    templates: List[TemplateMetadata]
    total: int
    page: int = 1
    page_size: Optional[int] = None
    total_pages: int = 1


class DeleteTemplateResponse(BaseModel):
    """Response for template deletion.

    Includes the deleted template's metadata and a confirmation message.
    """

    status: str
    message: str
    deleted_template: TemplateMetadata


class TemplateVersionMetadata(BaseModel):
    """One row of the template history panel (no JSON blobs)."""

    id: str
    template_id: str
    version_number: int
    name: str
    updated_by: Optional[str] = None
    change_source: str
    restored_from: Optional[int] = None
    created_at: datetime


class TemplateVersionDetail(TemplateVersionMetadata):
    """Full snapshot of one version, for diff rendering and rollback.

    Blobs are raw dicts on purpose: snapshots may predate the current
    ConfigurationModel shape, and the read path must always be able to
    show them. Normalization through today's models happens only on the
    rollback WRITE path (same pipeline as PUT).
    """

    flow: Dict[str, Any]
    configurations: Optional[Dict[str, Any]] = None
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None


class TemplateVersionListResponse(BaseModel):
    status: str = "success"
    versions: List[TemplateVersionMetadata]
    total: int
    active_version: Optional[int] = None


class RollbackTemplateResponse(BaseModel):
    status: str = "success"
    template_id: str
    restored_from: int
    new_version: int
    message: str
