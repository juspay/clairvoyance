"""The ``/uap`` prefix (agentic payments): one router, three sub-routers.

webhook_router     POST /webhook                       Juspay agent events
onboarding_router  /rider /onboarding /status /agents  the page
                   /agents/select /result              the page
                   POST /draw                          the chat's book_ticket tool
ticket_router      GET /ticket                         the chat's get_ticket tool
"""

from fastapi import APIRouter

from app.api.routers.breeze_buddy.uap.handlers import (
    onboarding_router,
    ticket_router,
    webhook_router,
)

router = APIRouter()
router.include_router(webhook_router)
router.include_router(onboarding_router)
router.include_router(ticket_router)
