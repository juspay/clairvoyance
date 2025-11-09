import json
import re
from enum import Enum

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.core.config import (
    AWS_VAYU_READ_API_KEY,
    AWS_VAYU_URL,
    AWS_VAYU_WRITE_API_KEY,
    DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR,
    DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR,
)
from app.core.logger import logger
from app.core.transport.http_client import create_http_client

from ..utils import _paisa_to_rupees
from .utils import (
    detect_surcharge_rule_overlaps,
    format_announcement_html,
    get_current_shop_config_data,
    optimize_rules_for_deletion,
    patch_shop_config,
    remove_html_tags,
    surcharge_rule_template,
    validate_and_process_surcharge_rules,
)

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None
shop_url: str | None = None
merchant_id: str | None = None
user_id: str | None = None

COD_PAYMENT_METHOD_TYPES = {"CASH", "UPI", "NB", "WALLET", "CARD", "*"}


async def get_surcharge_rules(params: FunctionCallParams):
    """Retrieves surcharge rules with an option to filter by payment method type. Values are in rupees."""
    payment_method_type = params.arguments.get("paymentMethodType", "*")

    # When paymentMethodType is "*", show ALL rules regardless of payment type
    if payment_method_type == "*":
        payment_type = "ALL"
    elif payment_method_type in COD_PAYMENT_METHOD_TYPES:
        payment_type = "COD"
    else:
        payment_type = "PARTIAL"

    # Check authentication first
    if not shop_id:
        await params.result_callback(
            {"error": "Authentication token is missing. Cannot get surcharge rules."}
        )
        return

    logger.info(
        f"Getting surcharge rules for shop {shop_id}, payment type: {payment_type}"
    )

    headers = {
        "Content-Type": "application/json",
        "Authorization": AWS_VAYU_READ_API_KEY,
    }

    endpoint = f"{AWS_VAYU_URL}/surcharge/rule?shopId={shop_id}"
    if payment_type != "ALL":
        endpoint += f"&paymentType={payment_type}"
    try:
        async with create_http_client() as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            result = response.json()

            rules = result.get("rules", [])
            if payment_type != "ALL" and rules:
                rules = [
                    rule for rule in rules if rule.get("paymentType") == payment_type
                ]

            # Convert paisa values back to rupees for user display
            converted_rules = []
            for rule in rules:
                converted_rule = rule.copy()
                # Convert minimumOrderValue and maximumOrderValue from paisa to rupees
                if "minimumOrderValue" in converted_rule:
                    converted_rule["minimumOrderValue"] = round(
                        _paisa_to_rupees(converted_rule["minimumOrderValue"])
                    )
                if "maximumOrderValue" in converted_rule:
                    converted_rule["maximumOrderValue"] = round(
                        _paisa_to_rupees(converted_rule["maximumOrderValue"])
                    )
                converted_rules.append(converted_rule)

            await params.result_callback(
                {
                    "success": True,
                    "data": converted_rules,
                    "paymentTypeFilter": payment_type,
                    "totalRules": len(converted_rules),
                }
            )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error fetching surcharge rules: {e.response.status_code} - {e.response.text}"
        )
        await params.result_callback(
            {
                "success": False,
                "error": f"API error: {e.response.status_code}",
                "details": e.response.text,
            }
        )
    except Exception as e:
        await params.result_callback(
            {"success": False, "error": f"An unexpected error occurred: {e}"}
        )


# Helper functions for the unified manage_vayu_surcharge function
async def _handle_create_surcharge_rule(params: FunctionCallParams):
    """Internal helper: Creates one or more new surcharge rules for a specific payment type. Ensures rules are unique and not already present."""
    rules = params.arguments.get("rules", [])
    payment_method_type = params.arguments.get("paymentMethodType", "*")

    if payment_method_type in COD_PAYMENT_METHOD_TYPES:
        payment_type = "COD"
    else:
        payment_type = "PARTIAL"

    payment_method = "*"

    if not rules:
        await params.result_callback(
            {
                "success": False,
                "error": "Rules array is required to create surcharge rules.",
            }
        )
        return

    if not all([shop_id, AWS_VAYU_WRITE_API_KEY, AWS_VAYU_URL]):
        await params.result_callback(
            {
                "success": False,
                "error": "Server misconfiguration. Missing required configuration.",
            }
        )
        return

    try:
        # Get existing rules to check for overlaps
        headers = {
            "Content-Type": "application/json",
            "Authorization": AWS_VAYU_READ_API_KEY,
        }
        endpoint = f"{AWS_VAYU_URL}/surcharge/rule?shopId={shop_id}"

        try:
            async with create_http_client() as client:
                response = await client.get(endpoint, headers=headers)
                response.raise_for_status()
                result = response.json()
                existing_rules_raw = result.get("rules", [])

                # Convert paisa to rupees for comparison
                existing_rules = []
                for rule in existing_rules_raw:
                    converted_rule = rule.copy()
                    if "minimumOrderValue" in converted_rule:
                        converted_rule["minimumOrderValue"] = _paisa_to_rupees(
                            converted_rule["minimumOrderValue"]
                        )
                    if "maximumOrderValue" in converted_rule:
                        converted_rule["maximumOrderValue"] = _paisa_to_rupees(
                            converted_rule["maximumOrderValue"]
                        )
                    existing_rules.append(converted_rule)
        except Exception as e:
            logger.warning(f"Could not retrieve existing rules: {e}")
            existing_rules = []

        # Check for overlaps with existing rules
        has_overlaps, overlap_details = detect_surcharge_rule_overlaps(
            rules, existing_rules, payment_type, payment_method_type, payment_method
        )

        if has_overlaps:
            error_message = (
                f"Cannot create rules due to overlaps: {'; '.join(overlap_details)}"
            )
            await params.result_callback({"success": False, "error": error_message})
            return

        # Add payment method details to each rule
        for rule in rules:
            if "paymentMethodType" not in rule:
                rule["paymentMethodType"] = payment_method_type
            if "paymentMethod" not in rule:
                rule["paymentMethod"] = payment_method

        # Validate and process rules
        success, result = validate_and_process_surcharge_rules(
            rules, payment_type, payment_method_type, payment_method
        )

        if not success:
            await params.result_callback({"success": False, "error": result})
            return

        api_rules = result
        headers["Authorization"] = AWS_VAYU_WRITE_API_KEY
        payload = {"rules": api_rules, "shopId": shop_id}
        endpoint = f"{AWS_VAYU_URL}/surcharge/rule/create"

        async with create_http_client(timeout=30.0) as client:
            response = await client.post(endpoint, json=payload, headers=headers)
            response.raise_for_status()
            result = response.json()

            enhanced_result = result.copy() if result else {}
            enhanced_result["rulesCreated"] = len(api_rules)

            await params.result_callback(
                {
                    "success": True,
                    "data": enhanced_result,
                    "message": f"Successfully created {len(api_rules)} surcharge rules for {payment_type}",
                }
            )

    except httpx.RequestError as e:
        logger.error(f"Network error creating surcharge rules: {e}")
        await params.result_callback(
            {
                "success": False,
                "error": "Network error occurred while creating surcharge rules",
                "details": str(e),
            }
        )
    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error creating surcharge rules: {e.response.status_code} - {e.response.text}"
        )
        await params.result_callback(
            {
                "success": False,
                "error": f"API error: {e.response.status_code}",
                "details": e.response.text,
            }
        )
    except Exception as e:
        logger.error(f"Unexpected error creating surcharge rules: {e}")
        await params.result_callback(
            {"success": False, "error": f"An unexpected error occurred: {e}"}
        )


async def _handle_delete_surcharge_rule(params: FunctionCallParams):
    """
    Internal helper: Optimized deletion that:
    1. Backs up original rules
    2. Sets target rule rate to 0
    3. Finds and merges adjacent zero rules
    4. Uses existing create function for validation and recreation
    5. Handles rollback on failure
    """
    logger.info(
        "OPTIMIZE DELETE FUNCTION CALLED - Advanced optimization will be performed"
    )
    rule_id = params.arguments.get("ruleId")
    rules = params.arguments.get("rules", [])
    logger.info("llm rules")
    logger.info(rules)
    if not rule_id:
        await params.result_callback(
            {"success": False, "error": "Rule ID is required for optimized deletion."}
        )
        return

    # Authentication check
    if not all([shop_id, AWS_VAYU_WRITE_API_KEY, AWS_VAYU_READ_API_KEY]):
        await params.result_callback(
            {
                "success": False,
                "error": "Authentication is missing for optimized deletion.",
            }
        )
        return

    try:
        # Step 1: First GET all existing rules (NOT DELETE!)
        headers = {
            "Content-Type": "application/json",
            "Authorization": AWS_VAYU_READ_API_KEY,
        }
        endpoint = f"{AWS_VAYU_URL}/surcharge/rule?shopId={shop_id}"

        async with create_http_client() as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            result = response.json()
            all_existing_rules = result.get("rules", [])

        if not all_existing_rules:
            await params.result_callback(
                {"success": False, "error": "No existing rules found for optimization."}
            )
            return

        # Step 2: Find target rule and extract payment details
        target_rule = None
        for rule in all_existing_rules:
            if rule.get("id") == rule_id:
                target_rule = rule
                break

        if not target_rule:
            await params.result_callback(
                {"success": False, "error": f"Target rule {rule_id} not found"}
            )
            return

        # Extract payment details from target rule
        payment_type = target_rule.get("paymentType")
        payment_method_type = target_rule.get("paymentMethodType")
        payment_method = target_rule.get("paymentMethod")

        logger.info(
            f"Starting optimized deletion for rule {rule_id} ({payment_type}-{payment_method_type}-{payment_method})"
        )

        # Step 3: Optimize rules (with all 5 required parameters)
        success, optimized_result, backup_data = optimize_rules_for_deletion(
            all_existing_rules,
            rule_id,
            payment_type,
            payment_method_type,
            payment_method,
        )

        if not success:
            await params.result_callback(
                {"success": False, "error": f"Optimization failed: {optimized_result}"}
            )
            return

        optimized_rules = optimized_result
        logger.info(
            f"Optimization successful: {len(optimized_rules)} rules after optimization"
        )

        rules_to_delete = [
            r
            for r in all_existing_rules
            if r.get("paymentType") == payment_type
            and r.get("paymentMethodType") == payment_method_type
        ]

        deleted_count = 0
        headers["Authorization"] = AWS_VAYU_WRITE_API_KEY

        for rule in rules_to_delete:
            if rule_id_to_delete := rule.get("id"):
                try:
                    delete_endpoint = (
                        f"{AWS_VAYU_URL}/surcharge/rule?ruleId={rule_id_to_delete}"
                    )
                    async with create_http_client() as client:
                        await client.delete(delete_endpoint, headers=headers)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete rule {rule_id_to_delete}: {e}")

        logger.info(f"Deleted {deleted_count} existing rules")

        # Step 4: Use existing create function to recreate optimized rules
        try:
            # Prepare rules for creation using the surcharge template
            rules_for_creation = []
            for rule in optimized_rules:
                create_rule = surcharge_rule_template(
                    payment_type=payment_type,
                    min_val=rule.get("minimumOrderValue", 0),
                    max_val=rule.get("maximumOrderValue"),
                    rate=rule.get("rate"),
                    rate_type=rule.get("rateType", "AMOUNT"),
                    payment_method_type=rule.get(
                        "paymentMethodType", payment_method_type
                    ),
                    payment_method=rule.get("paymentMethod", payment_method),
                )
                rules_for_creation.append(create_rule)

            if rules_for_creation:
                # Add payment method details to each rule
                for rule in rules_for_creation:
                    if "paymentMethodType" not in rule:
                        rule["paymentMethodType"] = payment_method_type
                    if "paymentMethod" not in rule:
                        rule["paymentMethod"] = payment_method

                # Validate and process
                success, result = validate_and_process_surcharge_rules(
                    rules_for_creation, payment_type, payment_method_type
                )

                if not success:
                    raise Exception(f"Validation failed: {result}")

                api_rules = result
                payload = {"rules": api_rules, "shopId": shop_id}
                create_endpoint = f"{AWS_VAYU_URL}/surcharge/rule/create"

                async with create_http_client(timeout=30.0) as client:
                    response = await client.post(
                        create_endpoint, json=payload, headers=headers
                    )
                    response.raise_for_status()
                    create_result = response.json()

                created_count = len(api_rules)
                logger.info(f"Successfully created {created_count} optimized rules")
            else:
                created_count = 0
                logger.info("No rules to create after optimization")

            # Step 5: Success response
            optimization_summary = (
                f"Optimized deletion completed: "
                f"deleted {deleted_count} rules, created {created_count} optimized rules"
            )

            await params.result_callback(
                {
                    "success": True,
                    "data": {
                        "rulesDeleted": deleted_count,
                        "rulesCreated": created_count,
                        "originalRuleCount": (
                            len(backup_data["rules"]) if backup_data else 0
                        ),
                        "optimizedRuleCount": created_count,
                        "rulesSaved": (len(backup_data["rules"]) if backup_data else 0)
                        - created_count,
                    },
                    "message": optimization_summary,
                    "backup": {
                        "timestamp": backup_data["timestamp"] if backup_data else None,
                        "originalRules": (
                            len(backup_data["rules"]) if backup_data else 0
                        ),
                    },
                }
            )

        except Exception as create_error:
            # Step 6: Rollback on creation failure
            logger.error(f"Creation failed, attempting rollback: {create_error}")

            try:
                # Restore from backup
                if backup_data and backup_data.get("rules"):
                    restore_rules = []
                    for rule in backup_data["rules"]:
                        restore_rule = {
                            "rate": rule.get("rate"),
                            "rateType": rule.get("rateType", "AMOUNT"),
                            "minimumOrderValue": _paisa_to_rupees(
                                rule.get("minimumOrderValue", 0)
                            ),
                            "maximumOrderValue": (
                                _paisa_to_rupees(rule.get("maximumOrderValue"))
                                if rule.get("maximumOrderValue") is not None
                                else None
                            ),
                            "paymentMethodType": rule.get(
                                "paymentMethodType", payment_method_type
                            ),
                            "paymentMethod": rule.get("paymentMethod", payment_method),
                        }
                        restore_rules.append(restore_rule)

                    # Use existing create function for rollback
                    from .utils import validate_and_process_surcharge_rules

                    success, result = validate_and_process_surcharge_rules(
                        restore_rules, payment_type, payment_method_type
                    )

                    if success:
                        api_rules = result
                        payload = {"rules": api_rules, "shopId": shop_id}

                        async with create_http_client(timeout=30.0) as client:
                            response = await client.post(
                                create_endpoint, json=payload, headers=headers
                            )
                            response.raise_for_status()

                        logger.info("Successfully restored from backup")
                        await params.result_callback(
                            {
                                "success": False,
                                "error": f"Optimization failed during creation, rules restored from backup: {str(create_error)}",
                                "rollback": "completed",
                            }
                        )
                    else:
                        raise Exception("Rollback validation failed")
                else:
                    raise Exception("No backup data available for rollback")

            except Exception as rollback_error:
                logger.error(f"Rollback failed: {rollback_error}")
                await params.result_callback(
                    {
                        "success": False,
                        "error": f"Optimization failed and rollback failed: {str(create_error)}. Manual intervention required.",
                        "rollback": "failed",
                        "rollbackError": str(rollback_error),
                    }
                )

    except httpx.HTTPStatusError as e:
        logger.error(
            f"HTTP error in optimized deletion: {e.response.status_code} - {e.response.text}"
        )
        await params.result_callback(
            {
                "success": False,
                "error": f"API error during optimized deletion: {e.response.status_code}",
                "details": e.response.text,
            }
        )
    except Exception as e:
        logger.error(f"Unexpected error in optimized deletion: {e}")
        await params.result_callback(
            {"success": False, "error": f"Optimized deletion failed: {str(e)}"}
        )


async def _handle_update_surcharge_rule(params: FunctionCallParams):
    """Internal helper: Simple update - delete all existing rules for payment type, then create new ones."""
    rules = params.arguments.get("rules", [])
    payment_method_type = params.arguments.get("paymentMethodType", "*")

    if payment_method_type in COD_PAYMENT_METHOD_TYPES:
        payment_type = "COD"
    else:
        payment_type = "PARTIAL"

    payment_method = "*"

    if not all([shop_id, AWS_VAYU_WRITE_API_KEY, AWS_VAYU_URL]):
        await params.result_callback(
            {
                "success": False,
                "error": "Server misconfiguration. Missing required configuration.",
            }
        )
        return

    # Basic rule validation
    if rules:
        for rule in rules:
            if "rate" not in rule or "rateType" not in rule:
                await params.result_callback(
                    {
                        "success": False,
                        "error": "Invalid rule format. Each rule must have rate and rateType.",
                    }
                )
                return

    try:
        # Validate new rules first
        api_rules = None
        if rules:
            for rule in rules:
                if "paymentMethodType" not in rule:
                    rule["paymentMethodType"] = payment_method_type
                if "paymentMethod" not in rule:
                    rule["paymentMethod"] = payment_method

            success, result = validate_and_process_surcharge_rules(
                rules, payment_type, payment_method_type, payment_method
            )
            if not success:
                await params.result_callback(
                    {"success": False, "error": f"Cannot update rules: {result}"}
                )
                return
            api_rules = result

        # Get existing rules to delete
        headers = {
            "Content-Type": "application/json",
            "Authorization": AWS_VAYU_READ_API_KEY,
        }
        endpoint = f"{AWS_VAYU_URL}/surcharge/rule?shopId={shop_id}"

        async with create_http_client() as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            existing_rules = response.json().get("rules", [])

        # Delete existing rules for this payment type
        rules_to_delete = [
            r
            for r in existing_rules
            if r.get("paymentType") == payment_type
            and r.get("paymentMethodType") == payment_method_type
            and r.get("paymentMethod") == payment_method
        ]
        deleted_count = 0

        headers["Authorization"] = AWS_VAYU_WRITE_API_KEY
        for rule in rules_to_delete:
            if rule_id := rule.get("id"):
                try:
                    delete_endpoint = f"{AWS_VAYU_URL}/surcharge/rule?ruleId={rule_id}"
                    async with create_http_client() as client:
                        await client.delete(delete_endpoint, headers=headers)
                    deleted_count += 1
                except Exception as e:
                    logger.warning(f"Failed to delete rule {rule_id}: {e}")

        # Create new rules if any
        created_count = 0
        if api_rules:
            payload = {"rules": api_rules, "shopId": shop_id}
            create_endpoint = f"{AWS_VAYU_URL}/surcharge/rule/create"

            async with create_http_client(timeout=30.0) as client:
                response = await client.post(
                    create_endpoint, json=payload, headers=headers
                )
                response.raise_for_status()
                created_count = len(api_rules)

        # Success response
        message = (
            f"Updated {payment_type} rules: deleted {deleted_count}, created {created_count}"
            if rules
            else f"Cleared all {payment_type} rules: deleted {deleted_count}"
        )

        await params.result_callback(
            {
                "success": True,
                "data": {"rulesDeleted": deleted_count, "rulesCreated": created_count},
                "message": message,
            }
        )

    except Exception as e:
        logger.error(f"Error updating surcharge rules: {e}")
        await params.result_callback(
            {"success": False, "error": f"Failed to update rules: {str(e)}"}
        )


# Unified surcharge management function
async def manage_vayu_surcharge(params: FunctionCallParams):
    """
    Unified function to manage surcharge rule operations (create, delete, update).
    The 'action' parameter determines which operation to perform.
    """
    action = params.arguments.get("action")

    if not action:
        await params.result_callback(
            {
                "success": False,
                "error": "Action is required. Use: create, delete, or update",
            }
        )
        return

    logger.info(f"Surcharge management operation started: {action}")

    if action == "create":
        await _handle_create_surcharge_rule(params)
    elif action == "delete":
        await _handle_delete_surcharge_rule(params)
    elif action == "update":
        await _handle_update_surcharge_rule(params)
    else:
        await params.result_callback(
            {
                "success": False,
                "error": f"Invalid action: {action}. Valid actions are: create, delete, update",
            }
        )


class BannerAction(str, Enum):
    FETCH = "fetch"
    ADD = "add"
    UPDATE = "update"
    REMOVE = "remove"


async def manage_announcement_banner(params: FunctionCallParams):
    """
    Manages all announcement banner operations including fetching, creating, updating, and deleting.

    Actions you can perform:
    - fetch: Retrieve the current announcement banner.
    - add: Create a new announcement banner.
    - update: Change the content of an existing announcement banner.
    - remove: Remove an announcement banner.

    The tool updates both login page and payment page announcements simultaneously.
    """
    try:
        action = params.arguments.get("action")
        description = params.arguments.get("description")
        background_color = params.arguments.get("background_color", None)
        text_color = params.arguments.get("text_color", None)

        # Validate action
        if not action:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Action is required.",
                }
            )
            return

        logger.info(f"Banner operation started: {action}")

        # For all actions, we need shop_url
        if not shop_url:
            logger.error(
                "Tool Error: [manage_announcement_banner] Banner operation called without required shop URL."
            )
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": "[manage_announcement_banner] Banner tool is not configured with shop information.",
                }
            )
            return

        # Fetch shop configuration data once
        try:
            config = await get_current_shop_config_data(shop_url)
            if background_color is None:
                banner = ""
                if isinstance(config, dict):
                    banner = config.get("announcementBannerText") or config.get(
                        "loginPageAnnouncementText", ""
                    )
                if isinstance(banner, str) and banner:
                    match = re.search(r"background:\s*([^;]+);?", banner)
                    if match:
                        background_color = match.group(1)
            if background_color is None:
                background_color = DEFAULT_ANNOUNCEMENT_BANNER_BACKGROUND_COLOR

            if text_color is None:
                banner = ""
                if isinstance(config, dict):
                    banner = config.get("announcementBannerText") or config.get(
                        "loginPageAnnouncementText", ""
                    )
                if isinstance(banner, str) and banner:
                    match = re.search(r"color:\s*([^;]+);?", banner)
                    if match:
                        text_color = match.group(1)
            if text_color is None:
                text_color = DEFAULT_ANNOUNCEMENT_BANNER_TEXT_COLOR

        except ValueError as e:
            logger.error(
                f"Tool Error: [manage_announcement_banner] Failed to get shop configuration: {str(e)}"
            )
            await params.result_callback(
                {
                    "success": False,
                    "error": f"Tool Error: [manage_announcement_banner] Could not retrieve shop configuration: {str(e)}",
                }
            )
            return

        # For fetch action, we only need shop_url
        if action == BannerAction.FETCH:
            # Extract announcement text from config (both keys have the same content)
            announcement_text = config.get(
                "loginPageAnnouncementText", ""
            ) or config.get("announcementBannerText", "")

            # Check if announcement is present
            if not announcement_text:
                # Prepare result indicating no banner is set
                result = {
                    "success": True,
                    "announcement": None,
                    "message": "No announcement banner is currently set for this merchant.",
                }
                logger.info(f"No announcement banner found for {shop_url}")
            else:
                clean_announcement = remove_html_tags(announcement_text)

                if not clean_announcement or len(clean_announcement) == 0:
                    result = {
                        "success": True,
                        "announcement": None,
                        "message": "No announcement content found.",
                    }
                    logger.info(f"No announcement content found for {shop_url}")
                else:
                    result = {"success": True, "announcement": clean_announcement}

                    logger.info(
                        f"Fetched announcement for {shop_url}: {clean_announcement}"
                    )

            # Return the result
            await params.result_callback(result)
            return

        # For all other actions, we need full shop information
        if not shop_id or not merchant_id or not breeze_token:
            logger.error(
                "Tool Error: [manage_announcement_banner] Banner operation called without required context (breezeToken, shopId, merchantId)."
            )
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Banner tool is not configured with shop information.",
                }
            )
            return

        # Validate description for add and update actions
        if (
            action == BannerAction.ADD or action == BannerAction.UPDATE
        ) and not description:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": " [manage_announcement_banner] Description is required when adding or updating a banner.",
                }
            )
            return

        config_to_patch = {**config}

        # Define both config keys that will be updated
        login_page_key = "loginPageAnnouncementText"
        payment_page_key = "announcementBannerText"

        if action == BannerAction.ADD or action == BannerAction.UPDATE:
            # Format the description with HTML styling
            formatted_description = format_announcement_html(
                description, background_color, text_color
            )

            # Update both announcement types with the same formatted content
            config_to_patch[login_page_key] = formatted_description
            config_to_patch[payment_page_key] = formatted_description

            logger.info(f"Setting both announcement types to '{formatted_description}'")
        elif action == BannerAction.REMOVE:
            # Remove both announcement types
            if login_page_key in config_to_patch:
                del config_to_patch[login_page_key]
            if payment_page_key in config_to_patch:
                del config_to_patch[payment_page_key]

            logger.info("Deleted both announcement types")

        if merchant_id == config.get("merchantId"):
            patch_result = await patch_shop_config(
                shop_url, user_id, config_to_patch, breeze_token
            )

            logger.info(
                f"Updated shop config for {shop_url} with changes to both announcement types"
            )
            logger.info(
                f"Config changes: {json.dumps({'loginPageAnnouncementText': config_to_patch.get('loginPageAnnouncementText'), 'announcementBannerText': config_to_patch.get('announcementBannerText')})}"
            )

            # If patch was successful, fetch and log the updated configuration
            if patch_result and patch_result.get("status") != "failure":
                try:
                    updated_config = await get_current_shop_config_data(shop_url)
                    logger.info(
                        f"Updated shop configuration: {json.dumps(updated_config)}"
                    )
                except ValueError as e:
                    logger.error(
                        f"Tool Error: [manage_announcement_banner] Failed to fetch updated configuration: {e}"
                    )

            # Return the patch_result
            await params.result_callback(patch_result)
        else:
            await params.result_callback(
                {
                    "success": False,
                    "Tool Error": "[manage_announcement_banner] Merchant ID mismatch. Cannot update configuration.",
                }
            )
            return

    except Exception as e:
        error_message = str(e)
        logger.error(f"Tool Error: [manage_announcement_banner] Error: {error_message}")
        await params.result_callback(
            {
                "success": False,
                "Tool Error": f" [manage_announcement_banner] Error: {error_message}",
            }
        )


manage_announcement_banner_function = FunctionSchema(
    name="manage_announcement_banner",
    description="""Manages all announcement banner operations including fetching, creating, updating, and deleting.

This tool can handle:
- **Login Page Announcements**: Simple text messages shown on the login page.
- **Payment Page Announcements**: Simple text messages shown on the payment page.

Actions you can perform:
- **fetch**: Retrieve the current announcement banner.
- **add**: Create a new announcement banner.
- **update**: Change the content of an existing announcement banner.
- **remove**: Remove an announcement banner.

For add and update actions, a description is required. Emojis are allowed in the banner content.

The tool updates both login page and payment page announcements simultaneously with the same content.
All announcements are formatted with HTML styling for consistent appearance.""",
    properties={
        "action": {
            "type": "string",
            "enum": [a.value for a in BannerAction],
            "description": "The operation to perform on the announcement banner: 'fetch' retrieves the current banner, 'add' creates a new banner, 'update' changes an existing banner's content, and 'remove' deletes the banner.",
        },
        "description": {
            "type": "string",
            "description": "The content of the banner. Required when creating or updating a banner. Emojis are allowed.",
        },
        "background_color": {
            "type": "string",
            "description": "The background color of the announcement banner. Default is #714acd (Light Purple).",
            "default": "#714acd",
        },
        "text_color": {
            "type": "string",
            "description": "The text color or font color of the announcement banner. Default is white.",
            "default": "white",
        },
    },
    required=["action"],
)

standard_tools = [manage_announcement_banner_function]
tool_functions = {"manage_announcement_banner": manage_announcement_banner}
get_surcharge_rules_function = FunctionSchema(
    name="get_surcharge_rules",
    description="Retrieves surcharge rules with optional filtering by payment type (COD, PARTIAL, or ALL). Order values are displayed in rupees.",
    properties={
        "paymentType": {
            "type": "string",
            "enum": ["COD", "PARTIAL", "ALL"],
            "description": "Filter surcharge rules by payment type. 'COD' for Cash on Delivery rules, 'PARTIAL' for partial payment rules, 'ALL' for all rules. Defaults to 'ALL' if not specified.",
        }
    },
    required=[],
)
#     - When in doubt, use CONVERT_TO_ZERO strategies instead of merging

manage_vayu_surcharge_function = FunctionSchema(
    name="manage_vayu_surcharge",
    description="""Manages surcharge rule operations for payment systems.

🚨 IMPORTANT: After ANY surcharge operation, ALWAYS call get_surcharge_rules() to display current rules with proper rupee values! 

Actions available:
- **create**: Add new surcharge rules with validation for overlaps and gaps
- **delete**: Intelligent deletion with adjacent zero-rule merging and optimization (automatically optimizes rules)
- **update**: Replace all rules for a payment type (delete existing + create new)

The delete action automatically performs optimization to minimize rules by merging adjacent zero-rate rules.""",
    properties={
        "action": {
            "type": "string",
            "enum": ["create", "delete", "update"],
            "description": "The operation to perform: 'create' adds new rules, 'delete' removes a specific rule with automatic optimization and rule merging, 'update' replaces all rules for a payment type.",
        },
        "rules": {
            "type": "array",
            "description": "🚨 MANDATORY FOR DELETE: Array of surcharge rule objects with the complete optimized rule set after deletion. Required for 'create', 'update', and 'delete' actions. For 'update', providing an empty array clears all rules for the payment type. For 'delete', NEVER omit this - provide the complete set of rules that should exist after the deletion.",
            "items": {
                "type": "object",
                "properties": {
                    "rate": {
                        "type": "number",
                        "description": "The surcharge rate. For AMOUNT type, this is a fixed amount. For PERCENTAGE type, this is the percentage (e.g., 2.5 = 2.5%).",
                    },
                    "rateType": {
                        "type": "string",
                        "enum": ["AMOUNT", "PERCENTAGE"],
                        "description": "Type of rate - AMOUNT for fixed amount, PERCENTAGE for percentage of order value. Defaults to AMOUNT if not specified.",
                    },
                    "minimumOrderValue": {
                        "type": "number",
                        "description": "Minimum order value for this rule to apply in RUPEES. Optional, defaults to 0. The system will convert this to paisa internally.",
                    },
                    "maximumOrderValue": {
                        "type": "number",
                        "description": "Maximum order value for this rule to apply in RUPEES. Optional, no limit if not specified. The system will convert this to paisa internally.",
                    },
                    "paymentMethodType": {
                        "type": "string",
                        "enum": ["CASH", "CARD", "NB", "UPI", "WALLET", "*"],
                        "description": "Primary payment method type this rule applies to. Use 'CASH' for cash payments, 'CARD' for CARD payments, 'NB' for net banking, 'UPI' for UPI payments, 'WALLET' for wallet payments, or '*' for ALL payment methods. The paymentMethod field will be automatically set based on this value.",
                    },
                    "paymentMethod": {
                        "type": "string",
                        "enum": ["CASH", "CREDIT/DEBIT", "NB", "UPI", "WALLET", "*"],
                        "description": "Internal payment method field - automatically derived from paymentMethodType. CASH→CASH, CREDIT/DEBIT→CREDIT/DEBIT, NB→NB, UPI→UPI, WALLET→WALLET, *→*. This field is auto-generated and optional to specify.",
                    },
                },
                "required": ["rate"],
            },
        },
        "ruleId": {
            "type": "string",
            "description": "The unique ID of the surcharge rule to delete. Required only for 'delete' action.",
        },
        "paymentType": {
            "type": "string",
            "enum": ["COD", "PARTIAL"],
            "description": "Payment type for the surcharge rules. 'COD' for Cash on Delivery rules, 'PARTIAL' for partial payment rules. Defaults to 'COD'.",
        },
    },
    required=["action"],
)

# Clean tool registration: 4 tools → 2 tools (matches manage_announcement_banner pattern)
standard_tools = [
    manage_announcement_banner_function,
    get_surcharge_rules_function,
    manage_vayu_surcharge_function,
]
tool_functions = {
    "manage_announcement_banner": manage_announcement_banner,
    "get_surcharge_rules": get_surcharge_rules,
    "manage_vayu_surcharge": manage_vayu_surcharge,
}
tools = ToolsSchema(standard_tools=standard_tools)
