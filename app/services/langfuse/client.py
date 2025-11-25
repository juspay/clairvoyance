"""
LangFuse service initialization.
Handles LangFuse client setup and configuration.
"""

from datetime import datetime
from typing import Any, Dict, Optional

import aiohttp
from langfuse import Langfuse

from app.core.config.static import (
    LANGFUSE_BASEURL,
    LANGFUSE_PUBLIC_KEY,
    LANGFUSE_SECRET_KEY,
)
from app.core.logger import logger




class LangFuseReadOnlyClient:
    """
    Read-only LangFuse client for score monitoring using REST API.

    This client uses the Langfuse REST API directly to fetch scores and traces.
    It does NOT use the SDK's tracing functionality, avoiding conflicts with
    the existing OTEL→Periscope→Langfuse tracing pipeline.

    SDK v3.8.1 is OpenTelemetry-based and doesn't have fetch_scores/fetch_trace
    methods, so we use the REST API instead.
    """

    def __init__(self):
        self.base_url: Optional[str] = None
        self.auth: Optional[aiohttp.BasicAuth] = None
        self.initialized = False
        self._http_client: Optional[aiohttp.ClientSession] = None

        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the read-only LangFuse client with error handling."""
        try:
            if not LANGFUSE_SECRET_KEY or not LANGFUSE_PUBLIC_KEY:
                logger.warning(
                    "Langfuse credentials not found. Score monitoring will not be available."
                )
                return

            # Set up REST API access
            self.base_url = LANGFUSE_BASEURL.rstrip("/")
            # Use Basic Auth with public_key:secret_key
            self.auth = aiohttp.BasicAuth(LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY)

            # Create async HTTP client
            timeout = aiohttp.ClientTimeout(total=30.0)
            self._http_client = aiohttp.ClientSession(
                base_url=self.base_url,
                auth=self.auth,
                timeout=timeout,
            )

            self.initialized = True
            logger.info(
                f"Langfuse read-only client initialized (base URL: {self.base_url})"
            )

        except Exception as e:
            logger.error(
                f"Failed to initialize Langfuse read-only client: {e}", exc_info=True
            )
            self.initialized = False


    async def close(self):
        """Close the async HTTP client."""
        if self._http_client:
            await self._http_client.close()
            logger.debug("Async HTTP client closed")


# Global client instances
langfuse_readonly_client = LangFuseReadOnlyClient()
