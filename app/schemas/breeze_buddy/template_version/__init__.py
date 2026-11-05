"""Schemas for template versioning (lineage) endpoints.

The public surface of the package — every consumer imports from here, never
from a submodule.

- ``lineage``  one template's version history + single rollback

Architecture: ``docs/TEMPLATE_LINEAGE.md``.
"""

from app.schemas.breeze_buddy.template_version.lineage import (
    RollbackTemplateRequest,
    TemplateVersionDetailResponse,
    TemplateVersionListResponse,
    TemplateVersionMetadata,
)

__all__ = [
    # Lineage (one template's history)
    "RollbackTemplateRequest",
    "TemplateVersionDetailResponse",
    "TemplateVersionListResponse",
    "TemplateVersionMetadata",
]
