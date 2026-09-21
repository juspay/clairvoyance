"""Per-template version history: the append-only lineage of one template.

Every write to a template appends a snapshot; these are the shapes the
``/templates/{id}/versions`` endpoints and the single-template rollback
speak. See ``docs/TEMPLATE_LINEAGE.md`` §3.2 and §8.1.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field

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
