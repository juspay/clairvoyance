import datetime
import pytz

from app.core.logger import logger
from app.tools.providers.system.chart_tools import (
    generate_bar_chart, generate_line_chart, generate_donut_chart
)

# ---- Function Declarations ----
get_current_time_declaration = {
    "name": "getCurrentTime",
    "description": "Get the current time in a specific timezone",
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "The timezone to get current time in (e.g., 'Asia/Kolkata', 'America/New_York'). Default is 'Asia/Kolkata' (India)"
            }
        },
        "required": []
    }
}

generate_bar_chart_declaration = {
    "name": "generate_bar_chart",
    "description": "Generate an interactive bar chart for comparing categories of data (e.g., payment methods, product performance, regional metrics)",
    "parameters": {
        "type": "object",
        "properties": {
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
        "required": ["title", "categories", "series_data", "voice_description"]
    }
}

generate_line_chart_declaration = {
    "name": "generate_line_chart", 
    "description": "Generate an interactive line chart for showing trends over time or sequences (e.g., sales trends, performance over months)",
    "parameters": {
        "type": "object",
        "properties": {
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
        "required": ["title", "categories", "series_data", "voice_description"]
    }
}

generate_donut_chart_declaration = {
    "name": "generate_donut_chart",
    "description": "Generate an interactive donut chart for showing proportions or percentages (e.g., payment method distribution, market share)",
    "parameters": {
        "type": "object",
        "properties": {
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
        "required": ["title", "categories", "data", "voice_description"]
    }
}

# ---- Tool Implementation Functions ----
def get_current_time(timezone="Asia/Kolkata"):
    """
    Get the current time in the specified timezone.
    """
    logger.info(f"SystemTool: getCurrentTime function called with timezone: {timezone}")
    try:
        tz = pytz.timezone(timezone)
        current_time = datetime.datetime.now(tz)
        logger.info(f"SystemTool: getCurrentTime result: {current_time.isoformat()}")
        return current_time.isoformat()
    except Exception as e:
        logger.error(f"SystemTool: Error in getCurrentTime: {e}")
        return f"Error: {str(e)}"

# ---- Rich Tool Definitions ----
system_tools_definitions = [
    {
        "declaration": get_current_time_declaration,
        "function": get_current_time,
        "required_context_params": [] # No extra context needed for this system tool
    },
    {
        "declaration": generate_bar_chart_declaration,
        "function": generate_bar_chart,
        "required_context_params": ["session_id"] # Need session_id for component storage
    },
    {
        "declaration": generate_line_chart_declaration,
        "function": generate_line_chart,
        "required_context_params": ["session_id"] # Need session_id for component storage
    },
    {
        "declaration": generate_donut_chart_declaration,
        "function": generate_donut_chart,
        "required_context_params": ["session_id"] # Need session_id for component storage
    }
]

__all__ = ["system_tools_definitions"]