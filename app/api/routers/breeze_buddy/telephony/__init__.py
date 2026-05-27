"""
Telephony-related endpoints.

This module contains telephony provider integrations and webhook handlers.

Sub-modules:
- callbacks: Webhook endpoints for telephony provider callbacks (status, recording)
- answer: Unified call answering endpoint for all providers (inbound, outbound, IVR)
"""

from fastapi import APIRouter

from .answer import router as answer_router
from .bridge import router as bridge_router
from .callbacks import router as callbacks_router

router = APIRouter()

# Include callbacks router (webhook endpoints)
router.include_router(callbacks_router, prefix="", tags=["telephony-callbacks"])

# Include answer router (unified call answering endpoint)
router.include_router(answer_router, prefix="", tags=["telephony-answer"])

# Include bridge router (Daily warm-transfer agent leg WebSocket)
router.include_router(bridge_router, prefix="", tags=["telephony-bridge"])
