"""Admin-only surfaces, mounted under ``/admin/*``.

Everything that only a platform admin may call lives in this package, one
sub-package per surface, and every route in here gates with
``require_admin``. Cross-tenant reads (fleet inventories, platform health)
and platform-wide actions belong here, never in a tenant-scoped router.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers.breeze_buddy.admin.ingest_archive import (
    router as admin_ingest_archive_router,
)
from app.api.routers.breeze_buddy.admin.templates import (
    router as admin_templates_router,
)

router = APIRouter()

# Template maintenance that the tenant endpoints cannot do: purge a template
# together with its chat history.
router.include_router(admin_templates_router, prefix="", tags=["admin-templates"])

# Replay archived ingest events from GCS through the events door (one pod
# POSTs to itself with each merchant's stored auth header).
router.include_router(
    admin_ingest_archive_router, prefix="", tags=["admin-ingest-archive"]
)

__all__ = ["router"]
