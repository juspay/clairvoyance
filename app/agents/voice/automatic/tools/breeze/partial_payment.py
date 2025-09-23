import json
from typing import Any, Dict, List, Optional

import httpx
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.services.llm_service import FunctionCallParams

from app.core.logger import logger

from .utils import call_vayu

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None
session_id: str | None = None
client_sid: str | None = None


async def _get_partial_payment_rules_data(
    shop_id: str, session_id: Optional[str], client_sid: Optional[str]
) -> Dict[str, Any]:
    """Internal helper to fetch partial payment rules."""
    return await call_vayu(
        method="GET",
        endpoint=f"/payment/partial/rule?shopId={shop_id}",
        session_id=session_id,
        request_id=client_sid,
    )


async def get_partial_payment_rules(params: FunctionCallParams):
    """
    Retrieves the complete set of partial payment rules configured for the current shop.
    """
    if not shop_id:
        await params.result_callback(
            {
                "success": False,
                "error": "Missing shop information in context. Shop ID is required.",
            }
        )
        return

    try:
        result = await _get_partial_payment_rules_data(shop_id, session_id, client_sid)
        await params.result_callback(
            {"success": True, "data": result.get("partialPaymentRule", [])}
        )
    except ValueError as e:
        logger.error(f"Error fetching partial payment rules: {e}")
        await params.result_callback({"success": False, "error": str(e)})


async def create_partial_payment_rule(params: FunctionCallParams):
    """
    Creates one or more new partial payment rules for the current shop.
    """
    if not shop_id:
        await params.result_callback(
            {
                "success": False,
                "error": "Missing shop information in context. Shop ID is required.",
            }
        )
        return

    try:
        # Check for existing rules by calling the internal helper
        existing_rules_response = await _get_partial_payment_rules_data(
            shop_id, session_id, client_sid
        )
        if existing_rules_response and existing_rules_response.get(
            "partialPaymentRule"
        ):
            error_message = "A partial payment rule already exists. Please update the existing rule or delete it before creating a new one."
            logger.warning(error_message)
            await params.result_callback({"success": False, "error": error_message})
            return

        rules = params.arguments.get("rules", [])
        payload = {
            "rules": [
                {
                    "minimumOrderValue": rule.get("minimumOrderValue", 0),
                    "maximumOrderValue": rule.get("maximumOrderValue"),
                    "rate": rule.get("rate"),
                    "rateType": rule.get("rateType"),
                }
                for rule in rules
            ],
            "shopId": shop_id,
        }

        result = await call_vayu(
            method="POST",
            endpoint="/payment/partial/rule/create",
            payload=payload,
            session_id=session_id,
            request_id=client_sid,
        )
        await params.result_callback({"success": True, "data": result})

    except ValueError as e:
        logger.error(f"Error creating partial payment rule: {e}")
        await params.result_callback({"success": False, "error": str(e)})


async def delete_partial_payment_rule(params: FunctionCallParams):
    """
    Deletes a specific partial payment rule from the current shop's configuration using its unique ID.
    """
    if not shop_id:
        await params.result_callback(
            {
                "success": False,
                "error": "Missing shop information in context. Shop ID is required.",
            }
        )
        return

    try:
        rule_id = params.arguments.get("ruleId")
        if not rule_id:
            await params.result_callback(
                {"success": False, "error": "ruleId is a required parameter."}
            )
            return

        result = await call_vayu(
            method="DELETE",
            endpoint=f"/payment/partial/rule/{rule_id}",
            session_id=session_id,
            request_id=client_sid,
        )
        await params.result_callback({"success": True, "data": result})

    except ValueError as e:
        logger.error(f"Error deleting partial payment rule: {e}")
        await params.result_callback({"success": False, "error": str(e)})


async def update_partial_payment_rules(params: FunctionCallParams):
    """
    Edits the entire set of partial payment rules for the current shop by replacing them.
    """
    if not shop_id:
        await params.result_callback(
            {
                "success": False,
                "error": "Missing shop information in context. Shop ID is required.",
            }
        )
        return

    try:
        # 1. Get all existing rules
        existing_rules_response = await _get_partial_payment_rules_data(
            shop_id, session_id, client_sid
        )
        existing_rules = existing_rules_response.get("partialPaymentRule", [])

        new_rules = params.arguments.get("rules", [])

        if new_rules:
            # 2. Create the new rules
            create_payload = {
                "rules": [
                    {
                        "minimumOrderValue": rule.get("minimumOrderValue", 0),
                        "maximumOrderValue": rule.get("maximumOrderValue"),
                        "rate": rule.get("rate"),
                        "rateType": rule.get("rateType"),
                    }
                    for rule in new_rules
                ],
                "shopId": shop_id,
            }
            create_result = await call_vayu(
                method="POST",
                endpoint="/payment/partial/rule/create",
                payload=create_payload,
                session_id=session_id,
                request_id=client_sid,
            )

            # 3. Delete all existing rules only if the new rules were created successfully
            if create_result:
                for rule in existing_rules:
                    await call_vayu(
                        method="DELETE",
                        endpoint=f"/payment/partial/rule/{rule['id']}",
                        session_id=session_id,
                        request_id=client_sid,
                    )

            await params.result_callback({"success": True, "data": create_result})
        else:
            # Match the JS code: if new_rules is empty, return success without deleting.
            await params.result_callback(
                {
                    "success": True,
                    "data": {
                        "status": "success",
                        "message": "All partial payment rules cleared successfully.",
                    },
                }
            )

    except ValueError as e:
        logger.error(f"Error updating partial payment rules: {e}")
        await params.result_callback({"success": False, "error": str(e)})


get_partial_payment_rules_function = FunctionSchema(
    name="get_partial_payment_rules",
    description="""Retrieves the complete set of partial payment rules configured for the current shop.""",
    properties={},
    required=[],
)

create_partial_payment_rule_function = FunctionSchema(
    name="create_partial_payment_rule",
    description="""Creates one or more new partial payment rules for the current shop. This tool will fail if any rules already exist; you must delete existing rules before creating new ones.""",
    properties={
        "rules": {
            "type": "array",
            "description": "An array of partial payment rule objects to be created.",
            "items": {
                "type": "object",
                "properties": {
                    "rate": {
                        "type": "number",
                        "description": "The rate for the rule. For 'AMOUNT' type, this should be in paise. For 'PERCENTAGE' type, it's the percentage value.",
                    },
                    "rateType": {
                        "type": "string",
                        "enum": ["AMOUNT", "PERCENTAGE"],
                        "description": "Specifies whether the rate is a fixed 'AMOUNT' or a 'PERCENTAGE'.",
                    },
                    "minimumOrderValue": {
                        "type": "number",
                        "description": "The minimum order value for this rule to apply, in paise. Optional.",
                    },
                    "maximumOrderValue": {
                        "type": "number",
                        "description": "The maximum order value for this rule to apply, in paise. Use null or omit for open-ended ranges. Optional.",
                    },
                },
                "required": ["rate", "rateType"],
            },
        }
    },
    required=["rules"],
)

delete_partial_payment_rule_function = FunctionSchema(
    name="delete_partial_payment_rule",
    description="""Deletes a specific partial payment rule from the current shop's configuration using its unique ID.""",
    properties={
        "ruleId": {"type": "string", "description": "The ID of the rule to delete."}
    },
    required=["ruleId"],
)

update_partial_payment_rules_function = FunctionSchema(
    name="update_partial_payment_rules",
    description="""Edits the entire set of partial payment rules for the current shop by replacing them. This is a "create-then-delete" operation.""",
    properties={
        "rules": {
            "type": "array",
            "description": "An array of new partial payment rule objects to replace the old ones. An empty array will delete all existing rules.",
            "items": {
                "type": "object",
                "properties": {
                    "rate": {
                        "type": "number",
                        "description": "The rate for the rule. For 'AMOUNT' type, this should be in paise. For 'PERCENTAGE' type, it's the percentage value.",
                    },
                    "rateType": {
                        "type": "string",
                        "enum": ["AMOUNT", "PERCENTAGE"],
                        "description": "Specifies whether the rate is a fixed 'AMOUNT' or a 'PERCENTAGE'.",
                    },
                    "minimumOrderValue": {
                        "type": "number",
                        "description": "The minimum order value for this rule to apply, in paise. Optional.",
                    },
                    "maximumOrderValue": {
                        "type": "number",
                        "description": "The maximum order value for this rule to apply, in paise. Use null or omit for open-ended ranges. Optional.",
                    },
                },
                "required": ["rate", "rateType"],
            },
        }
    },
    required=["rules"],
)

tools = ToolsSchema(
    standard_tools=[
        get_partial_payment_rules_function,
        create_partial_payment_rule_function,
        delete_partial_payment_rule_function,
        update_partial_payment_rules_function,
    ]
)
tool_functions = {
    "get_partial_payment_rules": get_partial_payment_rules,
    "create_partial_payment_rule": create_partial_payment_rule,
    "delete_partial_payment_rule": delete_partial_payment_rule,
    "update_partial_payment_rules": update_partial_payment_rules,
}
