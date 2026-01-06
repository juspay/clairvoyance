from fastapi import APIRouter

# Modern RESTful routers
from app.api.routers.breeze_buddy.analytics import router as analytics_router

# Auth, telephony, cron, websocket
from app.api.routers.breeze_buddy.auth import router as auth_router
from app.api.routers.breeze_buddy.configurations import router as configurations_router
from app.api.routers.breeze_buddy.cron import router as cron_router

# Daily transport (web/mobile clients via Daily.co)
from app.api.routers.breeze_buddy.daily import router as daily_router
from app.api.routers.breeze_buddy.deprecated import router as deprecated_router
from app.api.routers.breeze_buddy.leads import router as leads_router
from app.api.routers.breeze_buddy.merchants import router as merchants_router
from app.api.routers.breeze_buddy.numbers import router as numbers_router
from app.api.routers.breeze_buddy.telephony import router as telephony_router
from app.api.routers.breeze_buddy.templates import router as templates_router
from app.api.routers.breeze_buddy.websocket import router as websocket_router

router = APIRouter()

# ============================================================================
# Modern RESTful Endpoints (Primary)
# ============================================================================

# Authentication (JWT & S2S tokens)
router.include_router(auth_router, prefix="", tags=["auth"])

# Analytics (template-agnostic, RBAC-enabled)
router.include_router(analytics_router, prefix="", tags=["analytics"])

# Configurations (call execution configs)
router.include_router(configurations_router, prefix="", tags=["configurations"])

# Outbound Numbers (phone numbers for making calls)
router.include_router(numbers_router, prefix="", tags=["numbers"])

# Templates (conversational flow definitions)
router.include_router(templates_router, prefix="", tags=["templates"])

# Merchants (shop identifiers - admin only)
router.include_router(merchants_router, prefix="", tags=["merchants"])

# Leads (call requests/trackers)
router.include_router(leads_router, prefix="", tags=["leads"])

# Telephony (webhook handlers for call providers)
router.include_router(telephony_router, prefix="", tags=["telephony"])

# Cron (scheduled tasks)
router.include_router(cron_router, prefix="", tags=["cron"])

# WebSocket (real-time communication)
router.include_router(websocket_router, prefix="", tags=["websocket"])

# ============================================================================
# Deprecated Endpoints (Backward Compatibility)
# ============================================================================
# These endpoints maintain the old URL structure for backward compatibility.
# They will eventually be removed in a future version.
# Please migrate to the modern RESTful endpoints above.
router.include_router(deprecated_router, prefix="", tags=["deprecated"])
router.include_router(daily_router, prefix="", tags=["daily"])
