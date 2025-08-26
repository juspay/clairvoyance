import pytz
from datetime import datetime

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from app.core import config

# Conditionally import chart tools
if config.CHARTS_ENABLED:
    from app.tools.providers.system.chart_tools import (
        generate_bar_chart,
        generate_line_chart, 
        generate_donut_chart,
        generate_single_stat_card
    )

async def get_current_time(params: FunctionCallParams):
    timezone_str = params.arguments.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
        current_time = datetime.now(tz).isoformat()
        await params.result_callback({"time": current_time})
    except Exception as e:
        await params.result_callback({"error": str(e)})

get_current_time_function = FunctionSchema(
    name="get_current_time",
    description="Get the current time in a specific timezone.",
    properties={
        "timezone": {
            "type": "string",
            "description": "Timezone (e.g., 'Asia/Kolkata'). Defaults to 'Asia/Kolkata' if not specified.",
        }
    },
    required=[],
)

# Chart tool function schemas (only if charts are enabled)
if config.CHARTS_ENABLED:
    generate_bar_chart_function = FunctionSchema(
        name="generate_bar_chart",
        description="Generate an interactive bar chart for comparing categories of data",
        properties={
            "title": {
                "type": "string",
                "description": "Chart title (e.g., 'Payment Method Success Rates')"
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Category labels for x-axis (e.g., ['WALLET', 'CARD', 'UPI'])"
            },
            "series_data": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "data": {"type": "array", "items": {"type": "number"}},
                        "color": {"type": "string"}
                    },
                    "required": ["name", "data"]
                },
                "maxItems": 1,
                "description": "Data series - only one series allowed, pick the most relevant data"
            },
            "voice_description": {
                "type": "string",
                "description": "Natural language description for voice narration"
            },
            "subtitle": {
                "type": "string",
                "description": "Optional chart subtitle"
            }
        },
        required=["title", "categories", "series_data", "voice_description"]
    )

    generate_line_chart_function = FunctionSchema(
        name="generate_line_chart",
        description="Generate an interactive line chart for showing trends over time",
        properties={
            "title": {
                "type": "string",
                "description": "Chart title (e.g., 'Sales Trend Over Time')"
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Time/sequence labels for x-axis (e.g., ['Jan', 'Feb', 'Mar'])"
            },
            "series_data": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "data": {"type": "array", "items": {"type": "number"}},
                        "color": {"type": "string"}
                    },
                    "required": ["name", "data"]
                },
                "minItems": 1,
                "description": "Data series for trend lines - multiple series allowed for comparison"
            },
            "voice_description": {
                "type": "string",
                "description": "Natural language description for voice narration"
            },
            "subtitle": {
                "type": "string",
                "description": "Optional chart subtitle"
            }
        },
        required=["title", "categories", "series_data", "voice_description"]
    )

    generate_donut_chart_function = FunctionSchema(
        name="generate_donut_chart",
        description="Generate an interactive donut chart for showing proportions",
        properties={
            "title": {
                "type": "string",
                "description": "Chart title (e.g., 'Payment Method Distribution')"
            },
            "categories": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Category labels for segments (e.g., ['Credit Card', 'UPI', 'Wallet'])"
            },
            "data": {
                "type": "array",
                "items": {"type": "number"},
                "description": "Values for each category segment"
            },
            "data_type": {
                "type": "string",
                "enum": ["currency", "numericalValue", "percentage", "unknown"],
                "description": "Type of data values - currency (sum and show with ₹), numericalValue (sum normally), percentage (don't sum), unknown (no total shown)"
            },
            "voice_description": {
                "type": "string",
                "description": "Natural language description for voice narration"
            },
            "subtitle": {
                "type": "string",
                "description": "Optional chart subtitle"
            },
            "colors": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional custom colors for segments"
            }
        },
        required=["title", "categories", "data", "data_type", "voice_description"]
    )

    generate_single_stat_card_function = FunctionSchema(
        name="generate_single_stat_card",
        description="Generate a single statistic card showing a key metric",
        properties={
            "title": {
                "type": "string",
                "description": "Card title"
            },
            "primary_value": {
                "type": "number",
                "description": "Main numeric value to display (e.g., 24785640)"
            },
            "metric_name": {
                "type": "string",
                "description": "Name of the metric (e.g., 'MONTHLY REVENUE')"
            },
            "voice_description": {
                "type": "string",
                "description": "Natural language description for voice narration"
            },
            "delta_value": {
                "type": "string",
                "description": "Change value (e.g., '+5.2%')"
            },
            "delta_positive": {
                "type": "boolean",
                "description": "Whether delta is positive (default True)"
            },
            "date_range": {
                "type": "string",
                "description": "Time period for the metric (e.g., 'December 2024')"
            },
            "data_type": {
                "type": "string",
                "enum": ["currency", "numericalValue", "percentage", "unknown"],
                "description": "Type of primary_value - currency (format with ₹ and Indian numbering), numericalValue (Indian numbering), percentage (add % suffix), unknown (no formatting)",
                "default": "unknown"
            }
        },
        required=["title", "primary_value", "metric_name", "voice_description"]
    )

# Build tools list conditionally
standard_tools_list = [get_current_time_function]

if config.CHARTS_ENABLED:
    standard_tools_list.extend([
        generate_bar_chart_function,
        generate_line_chart_function,
        generate_donut_chart_function,
        generate_single_stat_card_function,
    ])

tools = ToolsSchema(standard_tools=standard_tools_list)

# Build tool functions dictionary conditionally
tool_functions = {"get_current_time": get_current_time}

if config.CHARTS_ENABLED:
    tool_functions.update({
        "generate_bar_chart": generate_bar_chart,
        "generate_line_chart": generate_line_chart,
        "generate_donut_chart": generate_donut_chart,
        "generate_single_stat_card": generate_single_stat_card,
    })