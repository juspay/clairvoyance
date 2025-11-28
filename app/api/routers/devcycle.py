"""
Optimized DevCycle Feature Flag Router

This module contains API endpoints for DevCycle feature flag management,
including webhook handling, health checks, and performance monitoring.
"""

import hmac
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from app.core.config.static import DEVCYCLE_WEBHOOK_SECRET
from app.core.logger import logger
from app.services.live_config.store import (
    fetch_and_update_feature_flags,
    get_flag_count,
)

router = APIRouter()


def verify_webhook_secret(provided_secret: Optional[str]) -> bool:
    """
    Securely verify the webhook secret using timing-safe comparison.

    Args:
        provided_secret: The secret provided in the query parameter

    Returns:
        bool: True if the secret is valid, False otherwise
    """
    if not DEVCYCLE_WEBHOOK_SECRET:
        logger.error("DEVCYCLE_WEBHOOK_SECRET is not configured")
        return False

    if not provided_secret:
        return False

    # Use timing-safe comparison to prevent timing attacks
    return hmac.compare_digest(DEVCYCLE_WEBHOOK_SECRET, provided_secret)


@router.post("/webhooks/devcycle")
async def devcycle_webhook(
    webhook_data: Dict[str, Any],
    request: Request,
    secret: Optional[str] = Query(None, description="Webhook authentication secret"),
):
    """
    Handle DevCycle feature flag updates via optimized webhook with authentication.

    This endpoint processes real-time DevCycle webhook updates and immediately
    updates the in-memory feature flag store and Redis cache. Requires a valid
    secret token provided as a query parameter.

    Args:
        webhook_data: The DevCycle webhook payload
        request: FastAPI request object
        secret: Authentication secret provided as query parameter

    Returns:
        JSONResponse: Success/error response with processing details

    Raises:
        HTTPException: 401 if authentication fails
    """
    webhook_start_time = time.time()
    client_ip = request.client.host if request.client else "unknown"

    # Authenticate the webhook request
    if not verify_webhook_secret(secret):
        logger.warning(
            f"DevCycle webhook authentication failed from {client_ip}. "
            f"Secret: {'[PRESENT]' if secret else '[MISSING]'}"
        )
        raise HTTPException(
            status_code=401, detail="Unauthorized: Invalid or missing webhook secret"
        )

    logger.info(
        f"DevCycle webhook authenticated from {client_ip}: {webhook_data.get('key', 'unknown')}"
    )

    try:
        # Fetch fresh configuration from DevCycle API and update store
        refresh_success = await fetch_and_update_feature_flags()

        if not refresh_success:
            logger.error("DevCycle webhook failed to refresh feature flags")
            return JSONResponse(
                status_code=500,
                content={
                    "status": "error",
                    "message": "Failed to refresh feature flags",
                },
            )

        logger.info("DevCycle webhook processed successfully")
        return JSONResponse(
            status_code=200,
            content={"status": "success", "message": "Feature flags updated"},
        )

    except Exception as e:
        logger.error(f"Failed to process DevCycle webhook: {e}")
        return JSONResponse(
            status_code=500, content={"status": "error", "message": str(e)}
        )
