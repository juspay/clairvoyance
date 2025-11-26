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
        webhook_processing_time = time.time() - webhook_start_time

        if not refresh_success:
            logger.error(
                f"DevCycle webhook failed to refresh feature flags from {client_ip} "
                f"in {webhook_processing_time*1000:.2f}ms"
            )
        else:
            # Extract webhook trigger info for logging
            webhook_key = webhook_data.get("key", "unknown")
            webhook_type = webhook_data.get("type", "unknown")

            logger.info(
                f"DevCycle webhook processed successfully: refreshed {await get_flag_count()} flags "
                f"triggered by {webhook_type} event for {webhook_key} in {webhook_processing_time*1000:.2f}ms"
            )

    except Exception as e:
        webhook_processing_time = time.time() - webhook_start_time
        logger.error(
            f"Failed to process DevCycle webhook from {client_ip}: {e}. "
            f"Processing time: {webhook_processing_time*1000:.2f}ms"
        )
