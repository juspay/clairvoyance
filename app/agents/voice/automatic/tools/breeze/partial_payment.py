import httpx
import json
from typing import List, Dict, Any, Optional

from app.core.logger import logger
from app.core.config import AWS_VAYU_URL, AWS_VAYU_READ_API_KEY
from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

# These will be set by the initializer
breeze_token: str | None = None
shop_id: str | None = None

async def get_partial_payment_rules(params: FunctionCallParams):
    """
    Retrieves the complete set of partial payment rules configured for the current shop.
    """
    if not shop_id:
        await params.result_callback({"success": False, "error": "Missing shop information in context. Shop ID is required."})
        return

    headers = {
        "Content-Type": "application/json",
        "Authorization": AWS_VAYU_READ_API_KEY
    }
    
    endpoint = f"{AWS_VAYU_URL}/payment/partial/rule?shopId={shop_id}"
    
    logger.info(f"Requesting partial payment rules from: {endpoint}")

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(endpoint, headers=headers)
            response.raise_for_status()
            result = response.json()
            logger.info(f"Received partial payment rules response status: {response.status_code}, data: {json.dumps(result)}")
            await params.result_callback({"success": True, "data": result.get("partialPaymentRule", [])})
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error fetching partial payment rules: {e.response.status_code} - {e.response.text}")
        await params.result_callback({"success": False, "error": f"API error: {e.response.status_code}", "details": e.response.text})
    except Exception as e:
        logger.error(f"Unexpected error fetching partial payment rules: {e}")
        await params.result_callback({"success": False, "error": f"An unexpected error occurred: {e}"})

get_partial_payment_rules_function = FunctionSchema(
    name="get_partial_payment_rules",
    description="""Retrieves the complete set of partial payment rules configured for the current shop.""",
    properties={},
    required=[]
)

tools = ToolsSchema(standard_tools=[get_partial_payment_rules_function])
tool_functions = {
    "get_partial_payment_rules": get_partial_payment_rules
}
