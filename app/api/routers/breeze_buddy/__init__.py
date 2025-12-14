from fastapi import APIRouter

from app.api.routers.breeze_buddy.auth import router as auth_router
from app.api.routers.breeze_buddy.call_execution_config import (
    router as call_execution_config_router,
)
from app.api.routers.breeze_buddy.callbacks import router as callbacks_router
from app.api.routers.breeze_buddy.dashboard import router as dashboard_router
from app.api.routers.breeze_buddy.leads import router as leads_router
from app.api.routers.breeze_buddy.outbound_numbers import (
    router as outbound_numbers_router,
)
from app.api.routers.breeze_buddy.template import router as template_router
from app.api.routers.breeze_buddy.websocket import router as websocket_router

router = APIRouter()

router.include_router(auth_router, prefix="", tags=["auth"])
router.include_router(template_router, prefix="", tags=["templates"])
router.include_router(
    outbound_numbers_router,
    prefix="",
    tags=["outbound-numbers"],
)
router.include_router(
    call_execution_config_router,
    prefix="",
    tags=["call-execution-config"],
)
router.include_router(leads_router, prefix="", tags=["leads"])
router.include_router(callbacks_router, prefix="", tags=["callbacks"])
router.include_router(dashboard_router, prefix="", tags=["dashboard"])
router.include_router(websocket_router, prefix="", tags=["websocket"])
