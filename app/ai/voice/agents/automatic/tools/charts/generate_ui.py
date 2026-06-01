from pipecat.adapters.schemas.function_schema import FunctionSchema

generate_bar_chart_function = FunctionSchema(
    name="generate_bar_chart",
    description="Generate an interactive bar chart for comparing categories of data",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Payment Method Success Rates')",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Category labels for x-axis (e.g., ['WALLET', 'CARD', 'UPI'])",
        },
        "series_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Concise name of the data series (e.g., 'Revenue', 'Orders', 'Customers')",
                    },
                    "data": {"type": "array", "items": {"type": "number"}},
                    "color": {"type": "string"},
                },
                "required": ["name", "data"],
            },
            "maxItems": 1,
            "description": "Data series - only one series allowed, pick the most relevant data",
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description for voice narration",
        },
        "subtitle": {"type": "string", "description": "Optional chart subtitle"},
    },
    required=["title", "categories", "series_data", "voice_description"],
)

generate_line_chart_function = FunctionSchema(
    name="generate_line_chart",
    description="Generate an interactive line chart for showing trends over time",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Sales Trend Over Time')",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Time/sequence labels for x-axis (e.g., ['Jan', 'Feb', 'Mar'])",
        },
        "series_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "data": {"type": "array", "items": {"type": "number"}},
                    "color": {"type": "string"},
                },
                "required": ["name", "data"],
            },
            "minItems": 1,
            "description": "Data series for trend lines - multiple series allowed for comparison",
        },
        "data_type": {
            "type": "string",
            "enum": ["currency", "numericalValue", "percentage", "unknown"],
            "description": "Type of data values - currency (format with ₹), numericalValue (format with K/L/Cr), percentage (show % and use last value in legend), unknown (no special formatting)",
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description for voice narration",
        },
        "subtitle": {"type": "string", "description": "Optional chart subtitle"},
    },
    required=["title", "categories", "series_data", "data_type", "voice_description"],
)

generate_donut_chart_function = FunctionSchema(
    name="generate_donut_chart",
    description="Generate an interactive donut chart for showing proportions",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Payment Method Distribution')",
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Category labels for segments (e.g., ['Credit Card', 'UPI', 'Wallet'])",
        },
        "data": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Values for each category segment",
        },
        "data_type": {
            "type": "string",
            "enum": ["currency", "numericalValue", "percentage", "unknown"],
            "description": "Type of data values - currency (sum and show with ₹), numericalValue (sum normally), percentage (don't sum), unknown (no total shown)",
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description for voice narration",
        },
        "subtitle": {"type": "string", "description": "Optional chart subtitle"},
        "colors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Optional custom colors for segments",
        },
    },
    required=["title", "categories", "data", "data_type", "voice_description"],
)

generate_single_stat_card_function = FunctionSchema(
    name="generate_single_stat_card",
    description="Generate a single statistic card showing a key metric",
    properties={
        "title": {"type": "string", "description": "Card title"},
        "primary_value": {
            "type": "number",
            "description": "Main numeric value to display (e.g., 24785640)",
        },
        "metric_name": {
            "type": "string",
            "description": "Name of the metric (e.g., 'MONTHLY REVENUE')",
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description for voice narration",
        },
        "delta_value": {
            "type": "string",
            "description": "Change value (e.g., '+5.2%')",
        },
        "delta_positive": {
            "type": "boolean",
            "description": "Whether delta is positive (default True)",
        },
        "date_range": {
            "type": "string",
            "description": "Time period for the metric (e.g., 'December 2024')",
        },
        "data_type": {
            "type": "string",
            "enum": ["currency", "numericalValue", "percentage", "unknown"],
            "description": "Type of primary_value - currency (format with ₹ and Indian numbering), numericalValue (Indian numbering), percentage (add % suffix), unknown (no formatting)",
            "default": "unknown",
        },
    },
    required=["title", "primary_value", "metric_name", "voice_description"],
)

standard_tools = [
    generate_bar_chart_function,
    generate_line_chart_function,
    generate_donut_chart_function,
    generate_single_stat_card_function,
]
