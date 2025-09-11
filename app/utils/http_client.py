"""
HTTP Client Factory with Proxy Support

This module provides a centralized way to create httpx.AsyncClient instances
with proxy configuration support. All external API calls should use this
factory to ensure consistent proxy usage across the application.
"""

import httpx
from typing import Optional, Any
from loguru import logger
from app.core.config import AWS_PROXY_HOST, AWS_PROXY_PORT, CLOUD_PROVIDER


def get_proxy_config() -> Optional[str]:
    """
    Get the proxy configuration string if proxy is configured and cloud provider is AWS.
    
    Returns:
        str: Proxy URL in format "http://host:port" or None if not configured or not AWS
    """
    # Only use proxy when CLOUD_PROVIDER is set to AWS
    if CLOUD_PROVIDER != "AWS":
        logger.debug(f"Cloud provider is '{CLOUD_PROVIDER}', not AWS. Skipping proxy configuration")
        return None
    
    if AWS_PROXY_HOST and AWS_PROXY_PORT:
        proxy_url = f"http://{AWS_PROXY_HOST}:{AWS_PROXY_PORT}"
        logger.debug(f"AWS cloud provider detected, using proxy configuration: {proxy_url}")
        return proxy_url
    else:
        logger.debug("AWS cloud provider detected but proxy not configured")
    
    return None


def create_http_client(
    timeout: Optional[float] = 30
) -> httpx.AsyncClient:
    """
    Create an httpx.AsyncClient with proxy support.
    
    Args:
        timeout: Request timeout in seconds
    
    Returns:
        httpx.AsyncClient: Configured HTTP client instance
    """
    # Use application's global proxy configuration
    proxy_url = get_proxy_config()
    
    client_kwargs = {
        "timeout": timeout
    }
    
    # Add proxy configuration if available
    if proxy_url:
        client_kwargs["proxy"] = proxy_url
        logger.debug(f"Creating HTTP client with proxy: {proxy_url}")
    else:
        logger.debug("Creating HTTP client without proxy")
    
    return httpx.AsyncClient(**client_kwargs)
