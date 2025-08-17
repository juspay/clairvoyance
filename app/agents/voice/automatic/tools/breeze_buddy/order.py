import httpx
from pipecat.services.llm_service import FunctionCallParams
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData
from app.core.config import ORDER_CONFIRMATION_ENDPOINT, ORDER_CONFIRMATION_TOKEN
from app.core.logger import logger

async def initiate_order_confirmation_call(params: FunctionCallParams):
    """
    Confirms a specific order and initiates a call to the customer.

    Args:
        params: The FunctionCallParams object containing the arguments.
    """
    print(f"Received params: {params.arguments}")
    order_data = params.arguments.get("order_data")
    if not order_data:
        await params.result_callback({"error": "order_data is a required parameter."})
        return

    url = ORDER_CONFIRMATION_ENDPOINT
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {ORDER_CONFIRMATION_TOKEN}",
    }
    
    # Assuming order_data can be directly converted to BreezeOrderData
    breeze_order_data = BreezeOrderData(**order_data)
    
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(url, headers=headers, json=breeze_order_data.model_dump(exclude_none=True))
            response.raise_for_status()
            await params.result_callback("I have initiated the call, check back your shopify dashboard for the confirmation result")
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error calling order confirmation API: {e.response.status_code} - {e.response.text}")
            await params.result_callback({"error": f"Order confirmation API error: {e.response.status_code}", "details": e.response.text})
        except Exception as e:
            logger.error(f"Unexpected error calling order confirmation API: {e}")
            await params.result_callback({"error": f"An unexpected error occurred: {e}"})
