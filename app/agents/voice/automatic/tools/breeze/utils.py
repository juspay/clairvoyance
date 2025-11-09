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

from app.core.config import (
    DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR,
    DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR,
    LIGHTHOUSE_APP_URL,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

from ..utils import _paisa_to_rupees, _rupees_to_paisa


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


def format_announcement_html(
    description: str,
    background_color: Optional[str] = None,
    text_color: Optional[str] = None,
) -> str:
    """
    Formats the announcement text with HTML styling.

    Args:
        description: The announcement text to format
        background_color: The background color of the announcement banner (defaults to config value)
        text_color: The text color of the announcement banner (defaults to config value)

    Returns:
        HTML formatted announcement text
    """
    # Use config defaults if not provided
    if background_color is None:
        background_color = DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR
    if text_color is None:
        text_color = DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR

    return f"<div style='text-align: center; width: 100vw;background: {background_color};color: {text_color};padding:8px 0px;font-size:13px;'>{description}</div>"


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
def detect_surcharge_rule_overlaps(
    new_rules, existing_rules, payment_type, payment_method_type, payment_method
):
    """
    Check if new rules overlap with existing rules OR within themselves for the same payment type and method type.
    Returns (has_overlaps, overlap_details) where overlap_details lists specific conflicts.
    """
    # Check overlaps with existing rules
    existing_payment_rules = [
        r
        for r in existing_rules
        if r.get("paymentType") == payment_type
        and r.get("paymentMethodType") == payment_method_type
        and r.get("paymentMethod") == payment_method
    ]

    overlaps = []

    # Check new rules against existing rules
    for new_rule in new_rules:
        new_min = new_rule.get("minimumOrderValue", 0)
        new_max = new_rule.get("maximumOrderValue")
        new_payment_method_type = new_rule.get("paymentMethodType", payment_method_type)

        for existing_rule in existing_payment_rules:
            existing_min = existing_rule.get("minimumOrderValue", 0)
            existing_max = existing_rule.get("maximumOrderValue")
            existing_payment_method_type = existing_rule.get("paymentMethodType")

            # Only check if payment method types match
            if (
                payment_method_type
                and existing_payment_method_type != new_payment_method_type
            ):
                continue

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
                    f"₹{new_min}-₹{new_max if new_max is not None else 'unlimited'} "
                    f"({new_payment_method_type})"
                )
                existing_range = (
                    f"₹{existing_min}-₹{existing_max if existing_max is not None else 'unlimited'} "
                    f"({existing_payment_method_type})"
                )
                overlaps.append(
                    f"New rule {new_range} overlaps with existing rule {existing_range}"
                )

    # Check internal overlaps within new rules
    temp_rules = [{"paymentType": payment_type, **rule} for rule in new_rules]
    filtered_rules = [
        r
        for r in temp_rules
        if r.get("paymentType") == payment_type
        and r.get("paymentMethodType", payment_method_type) == payment_method_type
        and r.get("paymentMethod") == payment_method
    ]
    sorted_rules = sorted(filtered_rules, key=lambda x: x.get("minimumOrderValue", 0))

    for i in range(len(sorted_rules) - 1):
        current = sorted_rules[i]
        next_rule = sorted_rules[i + 1]
        current_max = current.get("maximumOrderValue")
        next_min = next_rule.get("minimumOrderValue", 0)

        # True overlap (not just touching boundaries)
        if current_max is not None and current_max > next_min:
            pmt = current.get("paymentMethodType", payment_method_type)
            current_range = (
                f"₹{current.get('minimumOrderValue', 0)}-₹{current_max} ({pmt})"
            )
            next_range = f"₹{next_min}-₹{next_rule.get('maximumOrderValue', 'unlimited')} ({pmt})"
            overlaps.append(f"Rule {current_range} overlaps with {next_range}")

    return len(overlaps) > 0, overlaps


def surcharge_rule_template(
    payment_type,
    min_val,
    max_val,
    rate,
    rate_type="AMOUNT",
    payment_method="CASH",
    payment_method_type="CASH",
):
    """Helper function to create a surcharge rule with standard fields."""
    return {
        "paymentType": payment_type,
        "paymentMethod": payment_method,
        "paymentMethodType": payment_method_type,
        "applicationType": None,
        "amountFields": None,
        "logic": None,
        "minimumOrderValue": min_val,
        "maximumOrderValue": max_val,
        "rate": rate,
        "rateType": rate_type,
    }


def process_surcharge_input_rules(
    rules, payment_type, payment_method_type, payment_method
):
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
            payment_type,
            0,
            first_rule_min - 0.01,
            0,
            "AMOUNT",
            payment_method,
            payment_method_type,
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
            payment_method,  # Use derived value directly
            payment_method_type,  # Use derived value directly
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

            # Create unlimited rule for remaining range using same payment method settings
            unlimited_rule = surcharge_rule_template(
                payment_type,
                stored_max,
                None,
                0,
                "AMOUNT",
                payment_method,
                payment_method_type,
            )
            result.append(unlimited_rule)

    return result


def validate_and_process_surcharge_rules(
    rules, payment_type, payment_method_type, payment_method
):
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
        rules, [], payment_type, payment_method_type, payment_method
    )
    if has_overlaps:
        error_msg = f"Rules have overlaps: {'; '.join(overlap_details)}"
        logger.error(error_msg)
        return False, error_msg
    # Check for gaps (inline gap checking logic)
    temp_rules = [{"paymentType": payment_type, **rule} for rule in rules]
    payment_rules = [
        r
        for r in temp_rules
        if r.get("paymentType") == payment_type
        and r.get("paymentMethodType") == payment_method_type
        and r.get("paymentMethod") == payment_method
    ]

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
    processed_rules = process_surcharge_input_rules(
        rules, payment_type, payment_method_type, payment_method
    )

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


# Optimization functions for surcharge rule deletion
def find_adjacent_zero_rules(rules, payment_type, payment_method_type, payment_method):
    """
    Identify groups of adjacent zero-rate rules that can be merged.

    Args:
        rules: List of rules (in rupees format)
        payment_type: Payment type (COD/PARTIAL)
        payment_method_type: Payment method type
        payment_method: Payment method

    Returns:
        List of rule groups where each group contains adjacent zero-rate rules
    """
    # Filter rules for the specific payment type and method
    filtered_rules = [
        r
        for r in rules
        if r.get("paymentType") == payment_type
        and r.get("paymentMethodType") == payment_method_type
        and r.get("paymentMethod") == payment_method
        and r.get("rate") == 0
    ]

    if not filtered_rules:
        return []

    # Sort by minimum order value
    sorted_rules = sorted(filtered_rules, key=lambda x: x.get("minimumOrderValue", 0))

    adjacent_groups = []
    current_group = [sorted_rules[0]]

    for i in range(1, len(sorted_rules)):
        current_rule = sorted_rules[i]
        previous_rule = current_group[-1]

        # Check if rules are adjacent (previous max + 0.01 = current min)
        prev_max = previous_rule.get("maximumOrderValue")
        curr_min = current_rule.get("minimumOrderValue", 0)

        if prev_max is not None and abs(prev_max + 0.01 - curr_min) == 0:
            # Rules are adjacent, add to current group
            current_group.append(current_rule)
        else:
            # Rules are not adjacent, start new group
            if len(current_group) > 1:
                adjacent_groups.append(current_group)
            current_group = [current_rule]

    # Add the last group if it has multiple rules
    if len(current_group) > 1:
        adjacent_groups.append(current_group)

    logger.info(f"Found {len(adjacent_groups)} groups of adjacent zero rules")
    return adjacent_groups


def merge_adjacent_zero_rules(
    zero_rule_groups, payment_type, payment_method_type, payment_method
):
    """
    Merge adjacent zero-rate rules into optimized single rules.

    Args:
        zero_rule_groups: List of groups containing adjacent zero rules
        payment_type: Payment type
        payment_method_type: Payment method type
        payment_method: Payment method

    Returns:
        List of merged rules
    """
    merged_rules = []

    for group in zero_rule_groups:
        if len(group) <= 1:
            continue

        # Get the range from first rule's min to last rule's max
        first_rule = group[0]
        last_rule = group[-1]

        merged_rule = surcharge_rule_template(
            payment_type,
            first_rule.get("minimumOrderValue", 0),
            last_rule.get("maximumOrderValue"),
            0,
            first_rule.get("rateType", "AMOUNT"),
            payment_method,
            payment_method_type,
        )

        merged_rules.append(merged_rule)

        logger.info(
            f"Merged {len(group)} rules into: "
            f"₹{merged_rule['minimumOrderValue']}-"
            f"₹{merged_rule['maximumOrderValue'] if merged_rule['maximumOrderValue'] is not None else 'unlimited'}"
        )

    return merged_rules


def optimize_rules_for_deletion(
    all_rules, target_rule_id, payment_type, payment_method_type, payment_method
):
    """
    Main optimization function that:
    1. Sets target rule rate to 0
    2. Finds adjacent zero rules
    3. Merges them into optimized rules
    4. Returns the final optimized rule set

    Args:
        all_rules: All existing rules
        target_rule_id: ID of rule to be "deleted" (set to 0)
        payment_type: Payment type
        payment_method_type: Payment method type
        payment_method: Payment method

    Returns:
        Tuple of (success, optimized_rules_or_error_message, backup_data)
    """
    try:
        # Create backup first (inlined logic)
        filtered_rules = [
            r
            for r in all_rules
            if r.get("paymentType") == payment_type
            and r.get("paymentMethodType") == payment_method_type
            and r.get("paymentMethod") == payment_method
        ]

        backup = {
            "timestamp": int(time.time() * 1000),
            "payment_type": payment_type,
            "payment_method_type": payment_method_type,
            "payment_method": payment_method,
            "rules": filtered_rules,
            "total_rules": len(filtered_rules),
        }

        logger.info(
            f"Created backup for {payment_type}-{payment_method_type}: {len(filtered_rules)} rules"
        )

        # Find the target rule and set its rate to 0
        target_rule_found = False
        modified_rules = []

        for rule in all_rules:
            rule_copy = rule.copy()

            # Convert to rupees if needed for comparison
            if "minimumOrderValue" in rule_copy:
                rule_copy["minimumOrderValue"] = _paisa_to_rupees(
                    rule_copy["minimumOrderValue"]
                )
            if (
                "maximumOrderValue" in rule_copy
                and rule_copy["maximumOrderValue"] is not None
            ):
                rule_copy["maximumOrderValue"] = _paisa_to_rupees(
                    rule_copy["maximumOrderValue"]
                )

            # Check if this is the target rule
            if (
                rule.get("id") == target_rule_id
                and rule.get("paymentType") == payment_type
                and rule.get("paymentMethodType") == payment_method_type
                and rule.get("paymentMethod") == payment_method
            ):
                rule_copy["rate"] = 0
                target_rule_found = True
                logger.info(f"Set target rule {target_rule_id} rate to 0")

            modified_rules.append(rule_copy)

        if not target_rule_found:
            return False, f"Target rule {target_rule_id} not found", backup

        # Find adjacent zero rules
        adjacent_groups = find_adjacent_zero_rules(
            modified_rules, payment_type, payment_method_type, payment_method
        )

        if not adjacent_groups:
            logger.info("No adjacent zero rules found for merging")
            # Still return the modified rules (with target set to 0) even if no merging is possible
            filtered_rules = [
                r
                for r in modified_rules
                if r.get("paymentType") == payment_type
                and r.get("paymentMethodType") == payment_method_type
                and r.get("paymentMethod") == payment_method
            ]

            # Ultimate optimization: If only one rule remains with 0 rate, delete all rules
            if len(filtered_rules) == 1 and filtered_rules[0].get("rate") == 0:
                logger.info(
                    "ULTIMATE OPTIMIZATION: Single zero-rate rule detected - removing all rules"
                )
                logger.info(
                    f"Optimization complete: {len(backup['rules'])} → 0 rules (all rules eliminated)"
                )
                return True, [], backup

            return True, filtered_rules, backup

        # Merge adjacent zero rules
        merged_rules = merge_adjacent_zero_rules(
            adjacent_groups, payment_type, payment_method_type, payment_method
        )

        # Create final rule set: non-zero rules + merged zero rules
        final_rules = []
        # Get all rules that are not part of any adjacent group
        rules_in_groups = set()
        for group in adjacent_groups:
            for rule in group:
                rules_in_groups.add(id(rule))

        for rule in modified_rules:
            if (
                rule.get("paymentType") == payment_type
                and rule.get("paymentMethodType") == payment_method_type
                and rule.get("paymentMethod") == payment_method
            ):
                # Only include if not part of an adjacent group
                if id(rule) not in rules_in_groups:
                    final_rules.append(rule)

        # Add merged rules
        final_rules.extend(merged_rules)

        # Sort by minimum order value
        final_rules = sorted(final_rules, key=lambda x: x.get("minimumOrderValue", 0))

        # Ultimate optimization: If only one rule remains with 0 rate, delete all rules
        if len(final_rules) == 1 and final_rules[0].get("rate") == 0:
            logger.info(
                "ULTIMATE OPTIMIZATION: Single zero-rate rule detected - removing all rules"
            )
            logger.info(
                f"Optimization complete: {len(backup['rules'])} → 0 rules (all rules eliminated)"
            )
            return True, [], backup

        logger.info(
            f"Optimization complete: {len(backup['rules'])} → {len(final_rules)} rules"
        )
        return True, final_rules, backup

    except Exception as e:
        logger.error(f"Error in rule optimization: {e}")
        return False, f"Optimization failed: {str(e)}", None
