"""
Utility functions for Breeze shop operations including URL parsing, shop name extraction,
configuration management, and announcement formatting.
"""

import json
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.core.config import LIGHTHOUSE_APP_URL
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

from ..utils import _rupees_to_paisa


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


# Surcharge-specific utility functions
def detect_surcharge_rule_overlaps(new_rules, existing_rules, payment_type):
    """
    Check if new rules overlap with existing rules OR within themselves for the same payment type.
    Returns (has_overlaps, overlap_details) where overlap_details lists specific conflicts.
    """
    # Check overlaps with existing rules
    existing_payment_rules = [
        r for r in existing_rules if r.get("paymentType") == payment_type
    ]

    overlaps = []

    # Check new rules against existing rules
    for new_rule in new_rules:
        new_min = new_rule.get("minimumOrderValue", 0)
        new_max = new_rule.get("maximumOrderValue")

        for existing_rule in existing_payment_rules:
            existing_min = existing_rule.get("minimumOrderValue", 0)
            existing_max = existing_rule.get("maximumOrderValue")

            # Simple overlap check
            overlap_detected = False
            if existing_max is None:  # Existing rule is unlimited
                if new_min >= existing_min:
                    overlap_detected = True
            elif new_max is None:  # New rule is unlimited
                if new_min <= existing_min:
                    overlap_detected = True
            else:
                # Both have limits - check if they overlap
                if not (new_max < existing_min or new_min > existing_max):
                    overlap_detected = True

            if overlap_detected:
                new_range = (
                    f"₹{new_min}-₹{new_max if new_max is not None else 'unlimited'}"
                )
                existing_range = f"₹{existing_min}-₹{existing_max if existing_max is not None else 'unlimited'}"
                overlaps.append(
                    f"New rule {new_range} overlaps with existing rule {existing_range}"
                )

    # Check internal overlaps within new rules (combine _check_internal_overlaps logic)
    temp_rules = [{"paymentType": payment_type, **rule} for rule in new_rules]
    filtered_rules = [r for r in temp_rules if r.get("paymentType") == payment_type]
    sorted_rules = sorted(filtered_rules, key=lambda x: x.get("minimumOrderValue", 0))

    for i in range(len(sorted_rules) - 1):
        current = sorted_rules[i]
        next_rule = sorted_rules[i + 1]
        current_max = current.get("maximumOrderValue")
        next_min = next_rule.get("minimumOrderValue", 0)

        # True overlap (not just touching boundaries)
        if current_max is not None and current_max > next_min:
            current_range = f"₹{current.get('minimumOrderValue', 0)}-₹{current_max}"
            next_range = (
                f"₹{next_min}-₹{next_rule.get('maximumOrderValue', 'unlimited')}"
            )
            overlaps.append(f"Rule {current_range} overlaps with {next_range}")

    return len(overlaps) > 0, overlaps


def surcharge_rule_template(payment_type, min_val, max_val, rate, rate_type="AMOUNT"):
    """Helper function to create a surcharge rule with standard fields."""
    return {
        "paymentType": payment_type,
        "paymentMethod": "*",
        "paymentMethodType": "*",
        "applicationType": None,
        "amountFields": None,
        "logic": None,
        "minimumOrderValue": min_val,
        "maximumOrderValue": max_val,
        "rate": rate,
        "rateType": rate_type,
    }


def process_surcharge_input_rules(rules, payment_type):
    """
    SMART auto-completion handler for user rules:

    Case 1: "0-10, 10-20, 20-30, 30-null" → [0-9.99, 10-19.99, 20-29.99, 30-null]
    Case 2: "0-10, 10-20, 20-30" → [0-9.99, 10-19.99, 20-29.99, 30-null]
    Case 3: "0-1000" (single rule) → [0-999.99, 1000-null] (creates no-surcharge rule for remaining range)
    Case 4: "500-null" (starts above 0) → [0-499.99 (no surcharge), 500-null] (auto-fills gap from ₹0)

    Always creates complete coverage with no gaps.
    """
    if not rules:
        return rules

    # Sort rules by minimum value
    sorted_rules = sorted(rules, key=lambda x: x.get("minimumOrderValue", 0))
    result = []

    # STEP 0: Auto-fill gap from ₹0 if first rule doesn't start from ₹0
    first_rule_min = sorted_rules[0].get("minimumOrderValue", 0)
    if first_rule_min > 0:
        # Create no-surcharge rule from ₹0 to just before first rule starts
        no_surcharge_rule = surcharge_rule_template(
            payment_type, 0, first_rule_min - 0.01, 0, "AMOUNT"
        )
        result.append(no_surcharge_rule)

    # Process each rule and add required fields
    for rule in sorted_rules:
        new_rule = surcharge_rule_template(
            payment_type,
            rule.get("minimumOrderValue", 0),
            rule.get("maximumOrderValue"),
            rule.get("rate"),
            rule.get("rateType", "AMOUNT"),
        )
        result.append(new_rule)

    # STEP 1: Adjust all rules except the last one to end just before next rule starts
    for i in range(len(result) - 1):
        current_rule = result[i]
        next_rule = result[i + 1]
        original_max = current_rule.get("maximumOrderValue")
        next_min = next_rule.get("minimumOrderValue")

        if original_max is not None:
            adjusted_max = next_min - 0.01
            current_rule["maximumOrderValue"] = adjusted_max

    # STEP 2: Handle the last rule - handle defined max for both single and multiple rules
    if len(result) > 0:
        last_rule = result[-1]
        original_max = last_rule.get("maximumOrderValue")

        if original_max is not None:
            # Store original max for creating the unlimited rule
            stored_max = original_max
            # Adjust the last rule max
            last_rule["maximumOrderValue"] = stored_max - 0.01

            # Create unlimited rule for remaining range using helper function
            unlimited_rule = surcharge_rule_template(
                payment_type, stored_max, None, 0, "AMOUNT"
            )
            result.append(unlimited_rule)

    return result


def validate_and_process_surcharge_rules(rules, payment_type):
    """
    Validate, process and convert surcharge rules in one function:
    1. Check for overlaps in user input
    2. Check for gaps in user input
    3. Process and auto-complete the rules
    4. Convert to API format

    Returns (success, processed_rules_or_error_message)
    """
    if not rules:
        return False, "No rules provided"

    # Check for overlaps (both internal and with existing - using empty existing rules for internal check)
    has_overlaps, overlap_details = detect_surcharge_rule_overlaps(
        rules, [], payment_type
    )
    if has_overlaps:
        error_msg = f"Rules have overlaps: {'; '.join(overlap_details)}"
        logger.error(error_msg)
        return False, error_msg

    # Check for gaps (inline gap checking logic)
    temp_rules = [{"paymentType": payment_type, **rule} for rule in rules]
    payment_rules = [r for r in temp_rules if r.get("paymentType") == payment_type]

    if payment_rules:
        sorted_rules = sorted(
            payment_rules, key=lambda x: x.get("minimumOrderValue", 0)
        )
        gap_issues = []

        # Check for gaps between consecutive rules only (₹0 gap allowed since auto-filled)
        for i in range(len(sorted_rules) - 1):
            current_max = sorted_rules[i].get("maximumOrderValue")
            next_min = sorted_rules[i + 1].get("minimumOrderValue", 0)

            if current_max is not None and current_max + 1 < next_min:
                gap_start = current_max + 1
                gap_end = next_min - 1
                gap_issues.append(
                    f"Coverage gap: No rule covers orders from ₹{gap_start:.2f} to ₹{gap_end:.2f}"
                )

        if gap_issues:
            error_msg = f"Rules have gaps: {'; '.join(gap_issues)}"
            logger.error(error_msg)
            return False, error_msg

    # Process rules (fix boundaries and auto-complete)
    processed_rules = process_surcharge_input_rules(rules, payment_type)

    # Convert to API format (inline conversion logic)
    api_rules = []
    for rule in processed_rules:
        api_rule = rule.copy()
        # Convert only the order values to paisa
        if "minimumOrderValue" in api_rule:
            api_rule["minimumOrderValue"] = _rupees_to_paisa(
                api_rule["minimumOrderValue"]
            )
        if "maximumOrderValue" in api_rule:
            max_value = api_rule["maximumOrderValue"]
            if max_value is not None:
                api_rule["maximumOrderValue"] = _rupees_to_paisa(max_value)
            else:
                api_rule["maximumOrderValue"] = None
        api_rules.append(api_rule)

    logger.info(f"Successfully validated and processed {len(api_rules)} rules")
    return True, api_rules
