"""Admin-only surfaces, mounted under ``/admin/*``.

Everything that only a platform admin may call lives in this package, one
sub-package per surface, and every route in here gates with
``require_admin``. Cross-tenant reads (fleet inventories, platform health)
and platform-wide actions belong here, never in a tenant-scoped router.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routers.breeze_buddy.admin.assist_fleet import (
    router as assist_fleet_router,
)

router = APIRouter()

# Buddy Assist fleet inventory: every assist agent, usage, health, orphaned
# templates, shared-prompt variants — behind the console's Admin page.
router.include_router(assist_fleet_router, prefix="", tags=["admin-assist-fleet"])

__all__ = ["router"]
