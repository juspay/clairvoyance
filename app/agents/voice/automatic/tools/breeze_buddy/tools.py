from pipecat.adapters.schemas.function_schema import FunctionSchema
from app.agents.voice.breeze_buddy.breeze.order_confirmation.types import BreezeOrderData

order_data_schema = BreezeOrderData.schema()

initiate_order_confirmation_call_function = FunctionSchema(
    name="initiate_order_confirmation_call",
    description="Confirms a specific order and initiates a call to the customer. You must provide the complete order details for one of the orders returned by the get_shopify_orders tool.",
    properties={
        "order_data": {
            "type": "object",
            "description": "A dictionary containing the order details for a single order.",
            "properties": order_data_schema["properties"],
            "required": order_data_schema.get("required", []),
        },
    },
    required=["order_data"],
)

standard_tools = [initiate_order_confirmation_call_function]
