"""
LangFuse service initialization.
Handles LangFuse client setup and configuration.
"""

from typing import Optional
from langfuse import Langfuse
from app.core.logger import logger
from app.core.config import LANGFUSE_SECRET_KEY, LANGFUSE_PUBLIC_KEY, LANGFUSE_BASEURL


class LangFuseClient:
    """LangFuse client for managing connections and initialization."""

    def __init__(self):
        self.client: Optional[Langfuse] = None
        self.initialized = False

        self._initialize_client()

    def _initialize_client(self) -> None:
        """Initialize the LangFuse client with error handling."""
        try:
            if not LANGFUSE_SECRET_KEY or not LANGFUSE_PUBLIC_KEY:
                logger.warning("LangFuse credentials not found, using fallback prompts")
                return

            self.client = Langfuse(
                secret_key=LANGFUSE_SECRET_KEY,
                public_key=LANGFUSE_PUBLIC_KEY,
                host=LANGFUSE_BASEURL,
            )
            self.initialized = True
            logger.info("LangFuse client initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize LangFuse client: {e}")
            self.client = None

    def get_client(self) -> Optional[Langfuse]:
        """Get the initialized LangFuse client."""
        return self.client if self.initialized else None


# Global client instance
langfuse_client = LangFuseClient()
