"""Response schemas for template endpoints."""

from datetime import datetime
from typing import Dict, List, Optional

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
    workflow: str = "non-shopify"
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


class BatchConfigResultItem(BaseModel):
    """Per-template outcome of a batch configurations update.

    ``keys`` maps each requested dotted configuration path to its status:
    ``patched``, ``created``, ``skipped_not_present``, or ``unchanged_same_value``.
    """

    id: str
    reseller_id: str
    merchant_id: Optional[str] = None
    name: str
    workflow: str
    keys: Dict[str, str]


class BatchConfigResponse(BaseModel):
    """Response for a batch configurations update.

    With ``dry_run=True`` nothing is written; the per-template ``keys`` map is a
    preview of what would change.
    """

    dry_run: bool
    total_templates: int
    total_patched: int
    results: List[BatchConfigResultItem]
