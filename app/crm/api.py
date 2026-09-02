"""/crm root router (A5).

Mounted in app/main.py at the root — OUTSIDE /agent/voice/breeze-buddy
(ADR 0006). Each module's api.py router is included here as it lands:

    from app.crm.identity import api as identity_api
    router.include_router(identity_api.router, prefix="/customers")

Auth is per-module-router (admin routes depend on app.crm.auth.crm_admin_user;
ingest verifies signatures itself) — never blanket on this root router,
because webhook ingress must stay reachable without a bearer token.
"""

from fastapi import APIRouter

from app.crm.connectivity import api as connectivity_api
from app.crm.identity import api as identity_api
from app.crm.outreach import api as outreach_api
from app.crm.record import api as record_api

router = APIRouter()
router.include_router(identity_api.router, prefix="/customers", tags=["Customers"])
router.include_router(record_api.journey_router, prefix="/customers", tags=["Journey"])
router.include_router(record_api.ingest_router, prefix="/ingest", tags=["Ingest"])
router.include_router(outreach_api.router, prefix="/workflows", tags=["Workflows"])
router.include_router(
    outreach_api.customer_router, prefix="/customers", tags=["Workflows"]
)
router.include_router(
    connectivity_api.router, prefix="/connectors", tags=["Connectors"]
)
