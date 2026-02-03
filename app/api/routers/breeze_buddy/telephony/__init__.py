"""
Telephony-related endpoints.

This module contains telephony provider integrations and webhook handlers.

Sub-modules:
- callbacks: Webhook endpoints for telephony provider callbacks
- inbound: Inbound call handling with dynamic IVR
"""

from fastapi import APIRouter

from .callbacks import router as callbacks_router
from .inbound import router as inbound_router

router = APIRouter()

# Include callbacks router (webhook endpoints)
router.include_router(callbacks_router, prefix="", tags=["telephony-callbacks"])

# Include inbound router (IVR endpoints)
router.include_router(inbound_router, prefix="", tags=["telephony-inbound"])
