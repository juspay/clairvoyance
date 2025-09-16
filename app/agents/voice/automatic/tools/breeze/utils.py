"""
Utility functions for Breeze shop operations including URL parsing, shop name extraction,
configuration management, and announcement formatting.
"""

import json
import httpx
import time
import re
from enum import Enum
from typing import Optional, Dict, Any, List, TypedDict, Union
from urllib.parse import urlparse

from app.core.logger import logger
from app.core.config import LIGHTHOUSE_APP_URL
from app.core.transport.http_client import create_http_client


def safe_construct_url(url: str) -> Optional[urlparse]:
    """
    Safely parse a URL string into a urlparse object.

    Args:
        url: The URL string to parse

    Returns:
        Parsed URL object or None if parsing fails
    """
    try:
        parsed_url = urlparse(url)
        if parsed_url.netloc:  # Check if the URL has a valid host
            return parsed_url
        return None
    except Exception as e:
        logger.error(f"safeConstructUrlError: {str(e)}")
        return None


def is_shopify_shop(url: str) -> bool:
    """
    Check if a URL belongs to a Shopify shop.

    Args:
        url: The URL to check

    Returns:
        True if the URL contains 'myshopify', False otherwise
    """
    return "myshopify" in url if url else False


def get_shop_name_from_url(url: str) -> Optional[str]:
    """
    Extract shop name from a generic URL.

    Args:
        url: The shop URL

    Returns:
        Shop name or None if extraction fails
    """
    parsed_url = safe_construct_url(url)
    if parsed_url is None:
        return None

    host = re.sub(r"^www\.", "", parsed_url.netloc)
    shop_name = "-".join(host.split(".")[:-1])

    return shop_name if shop_name else None


def extract_shop_name(url: str) -> Optional[str]:
    """
    Extract shop name from a URL, handling both Shopify and non-Shopify URLs.

    Args:
        url: The shop URL

    Returns:
        Shop name or None if extraction fails
    """
    if not url:
        return None

    parsed_url = safe_construct_url(url)
    if parsed_url is None:
        return None

    if is_shopify_shop(url):
        parts = parsed_url.netloc.split(".")
        return parts[0] if parts else None
    else:
        return get_shop_name_from_url(url)


async def get_current_shop_config_data(shop_url: str) -> Dict[str, Any]:
    """
    Fetch current configuration data for a shop.

    Args:
        shop_url: URL of the shop

    Returns:
        Dictionary containing shop configuration data

    Raises:
        ValueError: If shop name extraction fails or configuration fetch fails
    """
    config_path = extract_shop_name(shop_url)

    if not config_path or len(config_path) == 0:
        logger.error(f"Invalid shop URL: Could not extract shop name from {shop_url}")
        raise ValueError("Invalid shop URL: Could not extract shop name")

    url = f"https://sdk.breeze.in/configs/{config_path}/config.json?timestamp={int(time.time() * 1000)}"

    try:
        logger.info(f"Fetching shop config from: {url}")
        async with create_http_client(timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            config_data = response.json()

            if not config_data:
                logger.error(f"Empty shop configuration received for {shop_url}")
                raise ValueError("Empty shop configuration received")

            return config_data
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error fetching shop config: {e.response.status_code} - {e.response.text}"
        )
        raise ValueError(
            f"Failed to fetch shop configuration: HTTP {e.response.status_code}"
        )
    except Exception as e:
        logger.error(f"getCurrentShopConfigDataExceptionOccured: {json.dumps(str(e))}")
        raise ValueError(f"Failed to fetch shop configuration: {str(e)}")


async def patch_shop_config(
    shop_url: str,
    user_id: str,
    config_data: Dict[str, Any],
    breeze_token: str,
    timeout: int = 30,
) -> Optional[Dict[str, Any]]:
    """
    Update shop configuration with provided data.

    Args:
        shop_url: URL of the shop
        user_id: ID of the user making the change
        config_data: Configuration data to update
        breeze_token: Authentication token
        timeout: Request timeout in seconds

    Returns:
        Response data from the API or error details dictionary
    """
    url = f"{LIGHTHOUSE_APP_URL}/shop/config"
    headers = {
        "Content-Type": "application/json",
        "x-shop-url": shop_url,
        "x-user-id": user_id,
        "x-auth-token": breeze_token,
    }
    try:
        async with create_http_client(timeout=timeout) as client:
            response = await client.patch(url, headers=headers, json=config_data)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error calling patch_shop_config: {e.response.status_code} - {e.response.text}"
        )
        return {
            "status": "failure",
            "message": f"Error calling patchShopConfig: {e}",
            "data": None,
            "statusCode": e.response.status_code,
        }
    except Exception as e:
        logger.error(f"Unexpected error calling patch_shop_config: {e}")
        return {
            "status": "failure",
            "message": f"Error calling patchShopConfig: {e}",
            "data": None,
            "statusCode": 500,
        }


def format_announcement_html(description: str) -> str:
    """
    Formats the announcement text with HTML styling.

    Args:
        description: The announcement text to format

    Returns:
        HTML formatted announcement text
    """
    return f"<div style='text-align: center; width: 100vw;background: green;color: white;padding:8px 0px;font-size:13px;'>{description}</div>"


def remove_html_tags(html_text: str) -> str:
    """
    Extracts text content between <div>...</div> and strips inner HTML tags.

    Args:
        html_text: The HTML-formatted text.

    Returns:
        Plain text string from inside the div tags.
    """
    if not html_text:
        return ""

    # Find everything between <div>...</div>
    match = re.search(r"<div.*?>(.*?)</div>", html_text, flags=re.DOTALL)

    if match:
        content = match.group(1)
    else:
        content = html_text
    clean_text = re.sub(r"<[^>]*>", "", content).strip()
    return clean_text
