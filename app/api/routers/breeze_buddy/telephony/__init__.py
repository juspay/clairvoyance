"""
Telephony-related endpoints.

This module contains telephony provider integrations and webhook handlers.

Sub-modules:
- callbacks: Webhook endpoints for telephony provider callbacks
"""

from fastapi import APIRouter

from .callbacks import router as callbacks_router

router = APIRouter()

# Include callbacks router (webhook endpoints)
router.include_router(callbacks_router, prefix="", tags=["telephony-callbacks"])
