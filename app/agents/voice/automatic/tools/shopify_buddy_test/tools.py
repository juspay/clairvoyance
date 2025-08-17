from pipecat.adapters.schemas.function_schema import FunctionSchema

standard_tools = [
    FunctionSchema(
        name="get_shopify_orders",
        description="Fetches order details from Shopify within a given date range.",
        properties={
            "start_date": {
                "type": "string",
                "description": "The start date in 'YYYY-MM-DDTHH:MM:SSZ' format.",
            },
            "end_date": {
                "type": "string",
                "description": "The end date in 'YYYY-MM-DDTHH:MM:SSZ' format.",
            },
        },
        required=["start_date", "end_date"],
    )
]
