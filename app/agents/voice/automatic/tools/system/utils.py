import pytz
from datetime import datetime
from typing import List, Dict, Any, Optional

from pipecat.services.llm_service import FunctionCallParams
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema

async def get_current_time(params: FunctionCallParams):
    timezone_str = params.arguments.get("timezone", "Asia/Kolkata")
    try:
        tz = pytz.timezone(timezone_str)
        current_time = datetime.now(tz).isoformat()
        await params.result_callback({"time": current_time})
    except Exception as e:
        await params.result_callback({"error": str(e)})


# Chart generation functions
async def generate_bar_chart(params: FunctionCallParams):
    """Generate a bar chart for comparative data analysis"""
    try:
        from app.tools.providers.system.chart_tools import generate_bar_chart as create_bar_chart
        
        # Extract arguments
        title = params.arguments.get("title", "")
        categories = params.arguments.get("categories", [])
        series_data = params.arguments.get("series_data", [])
        voice_description = params.arguments.get("voice_description", "")
        subtitle = params.arguments.get("subtitle")
        change_percentages = params.arguments.get("change_percentages")
        
        # Get session ID from logger context (configured in __init__.py) 
        session_id = "chart_session"  # Will be properly passed via Daily room context
        
        # Call the chart generation function
        result = create_bar_chart(
            title=title,
            categories=categories,
            series_data=series_data,
            voice_description=voice_description,
            subtitle=subtitle,
            change_percentages=change_percentages,
            session_id=session_id
        )
        
        await params.result_callback({"result": result})
    except Exception as e:
        await params.result_callback({"error": f"Error generating bar chart: {str(e)}"})


async def generate_line_chart(params: FunctionCallParams):
    """Generate a line chart for trend analysis"""
    try:
        from app.tools.providers.system.chart_tools import generate_line_chart as create_line_chart
        
        # Extract arguments
        title = params.arguments.get("title", "")
        categories = params.arguments.get("categories", [])
        series_data = params.arguments.get("series_data", [])
        voice_description = params.arguments.get("voice_description", "")
        subtitle = params.arguments.get("subtitle")
        
        # Get session ID from logger context (configured in __init__.py) 
        session_id = "chart_session"  # Will be properly passed via Daily room context
        
        # Call the chart generation function
        result = create_line_chart(
            title=title,
            categories=categories,
            series_data=series_data,
            voice_description=voice_description,
            subtitle=subtitle,
            session_id=session_id
        )
        
        await params.result_callback({"result": result})
    except Exception as e:
        await params.result_callback({"error": f"Error generating line chart: {str(e)}"})


async def generate_donut_chart(params: FunctionCallParams):
    """Generate a donut chart for distribution analysis"""
    try:
        from app.tools.providers.system.chart_tools import generate_donut_chart as create_donut_chart
        
        # Extract arguments
        title = params.arguments.get("title", "")
        categories = params.arguments.get("categories", [])
        data = params.arguments.get("data", [])
        voice_description = params.arguments.get("voice_description", "")
        subtitle = params.arguments.get("subtitle")
        
        # Get session ID from logger context (configured in __init__.py) 
        session_id = "chart_session"  # Will be properly passed via Daily room context
        
        # Call the chart generation function
        result = create_donut_chart(
            title=title,
            categories=categories,
            data=data,
            voice_description=voice_description,
            subtitle=subtitle,
            session_id=session_id
        )
        
        await params.result_callback({"result": result})
    except Exception as e:
        await params.result_callback({"error": f"Error generating donut chart: {str(e)}"})

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

generate_bar_chart_function = FunctionSchema(
    name="generate_bar_chart",
    description="Generate an interactive bar chart for comparing categories of data (e.g., payment methods, product performance, regional metrics)",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Payment Method Success Rates', 'Sales by Category')"
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of category labels for the x-axis (e.g., ['WALLET', 'CARD', 'UPI'])"
        },
        "series_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the data series (e.g., 'Success Rate (%)')"},
                    "data": {"type": "array", "items": {"type": "number"}, "description": "Data values corresponding to categories"},
                    "color": {"type": "string", "description": "Optional hex color for the series (e.g., '#1f77b4')"}
                },
                "required": ["name", "data"]
            },
            "description": "Data series for the chart"
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description highlighting key insights for voice narration (e.g., 'Payment methods chart showing CARD performing best at 78% success rate, while NB shows concerning 0% rate')"
        },
        "subtitle": {
            "type": "string",
            "description": "Optional chart subtitle for additional context"
        },
        "change_percentages": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Optional percentage changes for each category (e.g., [5.2, -2.1, 8.7])"
        }
    },
    required=["title", "categories", "series_data", "voice_description"]
)

generate_line_chart_function = FunctionSchema(
    name="generate_line_chart",
    description="Generate an interactive line chart for showing trends over time or sequences (e.g., sales trends, performance over months)",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Sales Trend Over Last 6 Months', 'Daily Order Volume')"
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of time/sequence labels for x-axis (e.g., ['Jan', 'Feb', 'Mar'] or ['Week 1', 'Week 2'])"
        },
        "series_data": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the trend line (e.g., 'Revenue', 'Orders')"},
                    "data": {"type": "array", "items": {"type": "number"}, "description": "Data values for the trend"},
                    "color": {"type": "string", "description": "Optional hex color for the line"}
                },
                "required": ["name", "data"]
            },
            "description": "Data series for trend lines"
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description of trends and patterns for voice narration"
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
    description="Generate an interactive donut chart for showing proportions or percentages (e.g., payment method distribution, market share)",
    properties={
        "title": {
            "type": "string",
            "description": "Chart title (e.g., 'Payment Method Distribution', 'Order Status Breakdown')"
        },
        "categories": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of category labels for segments (e.g., ['Credit Card', 'UPI', 'Wallet'])"
        },
        "data": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Values or percentages for each category segment"
        },
        "voice_description": {
            "type": "string",
            "description": "Natural language description of the distribution for voice narration"
        },
        "subtitle": {
            "type": "string",
            "description": "Optional chart subtitle"
        }
    },
    required=["title", "categories", "data", "voice_description"]
)

tools = ToolsSchema(
    standard_tools=[
        get_current_time_function,
        generate_bar_chart_function,
        generate_line_chart_function,
        generate_donut_chart_function,
    ]
)

tool_functions = {
    "get_current_time": get_current_time,
    "generate_bar_chart": generate_bar_chart,
    "generate_line_chart": generate_line_chart,
    "generate_donut_chart": generate_donut_chart,
}