"""
Langfuse Trace Fetching

This module provides functionality to fetch trace data from Langfuse via REST API.
"""

from typing import Any, Dict

import aiohttp

from app.core.logger import logger


async def fetch_trace(http_client: aiohttp.ClientSession, trace_id: str) -> Dict[str, Any]:
    """
    Fetch a trace from Langfuse via REST API.

    Args:
        http_client: Initialized aiohttp client session with auth and base URL
        trace_id: The ID of the trace to fetch

    Returns:
        Dictionary with trace data

    Raises:
        aiohttp.ClientResponseError: If API request fails
    """
    logger.debug(f"Fetching trace: {trace_id}")

    # Make API request
    try:
        async with http_client.get(f"/api/public/traces/{trace_id}") as response:
            response.raise_for_status()
            return await response.json()
    except aiohttp.ClientResponseError as e:
        logger.error(f"HTTP {e.status} error fetching trace {trace_id}")
        logger.error(f"Request URL: {e.request_info.url}")
        logger.error(f"Response body: {e.message}")
        raise
