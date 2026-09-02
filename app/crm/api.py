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

from app.crm.connectivity import (
    api as connectivity_api,
    contracts as connectivity_contracts,
)
from app.crm.identity import api as identity_api
from app.crm.outreach import api as outreach_api
from app.crm.record import api as record_api, ingress as record_ingress

router = APIRouter()
router.include_router(identity_api.router, prefix="/customers", tags=["Customers"])
router.include_router(record_api.journey_router, prefix="/customers", tags=["Journey"])
router.include_router(record_api.ingest_router, prefix="/ingest", tags=["Ingest"])
# The provider bays beside the envelope door above (design/ingest-doors: one
# mailroom, two entrance kinds). These routes carry NO bearer auth — each
# provider authenticates by its own signature ritual inside its registered
# bay, so /ingest/webhooks/{provider} must stay reachable without a token.
router.include_router(
    record_api.webhook_router, prefix="/ingest/webhooks", tags=["Ingest"]
)
# The one registration line per provider bay — the same line worker_main
# writes for consumers, and the inversion that keeps rule 12 whole: record
# owns the slot, this root fills it, and record never imports back.
record_ingress.register_ingress("meta", connectivity_contracts.META_INGRESS)
router.include_router(outreach_api.router, prefix="/workflows", tags=["Workflows"])
router.include_router(
    outreach_api.customer_router, prefix="/customers", tags=["Workflows"]
)
router.include_router(
    connectivity_api.router, prefix="/connectors", tags=["Connectors"]
)
