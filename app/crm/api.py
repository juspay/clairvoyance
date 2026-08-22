"""/crm root router (A5).

Mounted in app/main.py at prefix "/crm" — OUTSIDE /agent/voice/breeze-buddy
(ADR 0006). Each module's api.py router is included here as it lands:

    from app.crm.identity import api as identity_api
    router.include_router(identity_api.router, prefix="/customers")

Auth is per-module-router (admin routes depend on app.crm.auth.crm_admin_user;
ingest verifies signatures itself) — never blanket on this root router,
because webhook ingress must stay reachable without a bearer token.
"""

from fastapi import APIRouter

from app.crm.identity import api as identity_api

router = APIRouter()
router.include_router(identity_api.router, prefix="/customers", tags=["CRM Customers"])
