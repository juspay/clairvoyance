from fastapi import APIRouter

# Pod health probes (1-pod-1-call isolation architecture)
from app.api.routers.breeze_buddy.agent_router.health import router as pod_router

# Modern RESTful routers
from app.api.routers.breeze_buddy.analytics import router as analytics_router

# Auth, telephony, cron, websocket
from app.api.routers.breeze_buddy.auth import router as auth_router
from app.api.routers.breeze_buddy.blacklist import router as blacklist_router
from app.api.routers.breeze_buddy.configurations import router as configurations_router
from app.api.routers.breeze_buddy.credentials import router as credentials_router
from app.api.routers.breeze_buddy.cron import router as cron_router

# Daily transport (web/mobile clients via Daily.co)
from app.api.routers.breeze_buddy.daily import router as daily_router
from app.api.routers.breeze_buddy.demo import router as demo_router
from app.api.routers.breeze_buddy.leads import router as leads_router
from app.api.routers.breeze_buddy.merchants import router as merchants_router
from app.api.routers.breeze_buddy.numbers import router as numbers_router
from app.api.routers.breeze_buddy.telephony import router as telephony_router
from app.api.routers.breeze_buddy.templates import router as templates_router
from app.api.routers.breeze_buddy.users import router as users_router
from app.api.routers.breeze_buddy.websocket import router as websocket_router

router = APIRouter()

# ============================================================================
# Modern RESTful Endpoints (Primary)
# ============================================================================

# Public demo (unauthenticated)
router.include_router(demo_router, prefix="", tags=["demo"])

# Authentication (JWT & S2S tokens)
router.include_router(auth_router, prefix="", tags=["auth"])

# Analytics (template-agnostic, RBAC-enabled)
router.include_router(analytics_router, prefix="", tags=["analytics"])

# Configurations (call execution configs)
router.include_router(configurations_router, prefix="", tags=["configurations"])

# Credentials (API keys, tokens - centralized secret management)
router.include_router(credentials_router, prefix="", tags=["credentials"])

# Outbound Numbers (phone numbers for making calls)
router.include_router(numbers_router, prefix="", tags=["numbers"])

# Templates (conversational flow definitions)
router.include_router(templates_router, prefix="", tags=["templates"])

# Blacklist (blocked phone numbers - admin only)
router.include_router(blacklist_router, prefix="", tags=["blacklist"])

# Merchants (shop identifiers - admin only)
router.include_router(merchants_router, prefix="", tags=["merchants"])

# User Accounts (login accounts with RBAC)
router.include_router(users_router, prefix="", tags=["users"])

# Leads (call requests/trackers)
router.include_router(leads_router, prefix="", tags=["leads"])

# Telephony (webhook handlers for call providers)
router.include_router(telephony_router, prefix="", tags=["telephony"])

# Cron (scheduled tasks)
router.include_router(cron_router, prefix="", tags=["cron"])

# WebSocket (real-time communication)
router.include_router(websocket_router, prefix="", tags=["websocket"])

# Pod management (1-pod-1-call isolation for Kubernetes)
router.include_router(pod_router, prefix="/pod", tags=["pod"])

router.include_router(daily_router, prefix="", tags=["daily"])
