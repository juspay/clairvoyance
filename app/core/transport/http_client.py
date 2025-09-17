"""
Centralized HTTP client factory with proxy support for both httpx and aiohttp
"""

from typing import Optional

import aiohttp
import httpx

from app.core import config
from app.core.logger import logger


def get_proxy_config() -> Optional[str]:
    """Get proxy configuration from environment variables"""
    # Only use proxy configuration for AWS cloud environment
    if config.CLOUD_ENVIRONMENT.upper() != "AWS":
        logger.debug(
            f"Skipping proxy configuration for cloud environment: {config.CLOUD_ENVIRONMENT}"
        )
        return None

    if config.AWS_PROXY_HOST and config.AWS_PROXY_PORT:
        proxy_url = f"http://{config.AWS_PROXY_HOST}:{config.AWS_PROXY_PORT}"
        logger.info(f"Using proxy configuration for AWS environment: {proxy_url}")
        return proxy_url

    logger.debug("No proxy configuration found for AWS environment")
    return None


def create_http_client(**kwargs) -> httpx.AsyncClient:
    """
    Create an httpx AsyncClient with proxy support

    Args:
        **kwargs: Additional arguments to pass to httpx.AsyncClient

    Returns:
        httpx.AsyncClient: Configured HTTP client
    """
    proxy_url = get_proxy_config()

    client_kwargs = kwargs.copy()
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
        logger.debug(f"Created httpx client with proxy: {proxy_url}")
    else:
        logger.debug("Created httpx client without proxy")

    return httpx.AsyncClient(**client_kwargs)


def create_aiohttp_session(**session_kwargs) -> aiohttp.ClientSession:
    """
    Create an aiohttp.ClientSession with proxy support

    Args:
        **session_kwargs: Additional arguments to pass to aiohttp.ClientSession

    Returns:
        aiohttp.ClientSession: Configured aiohttp session
    """
    proxy_url = get_proxy_config()

    # Use aiohttp's built-in proxy parameter in the constructor
    if proxy_url:
        session_kwargs["proxy"] = proxy_url
        logger.debug(f"Created aiohttp session with proxy: {proxy_url}")
    else:
        logger.debug("Created aiohttp session without proxy")

    return aiohttp.ClientSession(**session_kwargs)
