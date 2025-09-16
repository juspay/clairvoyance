"""
Navigation tools schema for LLM function calling.
Defines the tool specifications for chart navigation functions.
"""

from pipecat.adapters.schemas.function_schema import FunctionSchema

# Chart navigation tool schemas
standard_tools = [
    FunctionSchema(
        name="navigate_to_chart",
        description="Navigate to a specific chart by index (1-based for user, 0-based internally) or ID. Use this when user asks to go to a specific chart number or ID.",
        properties={
            "chart_index": {
                "type": "integer",
                "description": "The chart index (0-based) to navigate to. Convert user's 1-based numbers to 0-based."
            },
            "chart_id": {
                "type": "string", 
                "description": "The chart ID to navigate to (alternative to index)"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID (optional, will use current session if not provided)"
            }
        },
        required=[]
    ),
    
    FunctionSchema(
        name="search_charts",
        description="Search for charts by title, type, categories, or series names. Use this when user asks to find charts with specific content or characteristics.",
        properties={
            "search_query": {
                "type": "string",
                "description": "The search query to find matching charts"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID (optional, will use current session if not provided)"
            }
        },
        required=["search_query"]
    ),
    
    FunctionSchema(
        name="list_charts", 
        description="List all available charts with their basic information. Use this when user asks how many charts there are or wants to see all charts.",
        properties={
            "session_id": {
                "type": "string",
                "description": "Session ID (optional, will use current session if not provided)"
            }
        },
        required=[]
    ),
    
    FunctionSchema(
        name="get_chart_info",
        description="Get detailed information about a specific chart including title, type, categories, and series. Use this when user asks for details about a specific chart.",
        properties={
            "chart_index": {
                "type": "integer", 
                "description": "The chart index (0-based) to get info for"
            },
            "chart_id": {
                "type": "string",
                "description": "The chart ID to get info for (alternative to index)"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID (optional, will use current session if not provided)"
            }
        },
        required=[]
    ),
    
    FunctionSchema(
        name="summarize_charts",
        description="Combine and summarize multiple charts into a single AI-generated chart. Use this when user asks to summarize, combine, or merge specific charts.",
        properties={
            "chart_indices": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Array of chart indices (0-based) to summarize. Convert user's 1-based numbers to 0-based."
            },
            "summary_type": {
                "type": "string",
                "enum": ["auto", "comparison", "trend", "aggregate"],
                "description": "Type of summary: auto (AI decides), comparison (side-by-side), trend (time-based), aggregate (combined totals)"
            },
            "session_id": {
                "type": "string",
                "description": "Session ID (optional, will use current session if not provided)"
            }
        },
        required=["chart_indices"]
    )
]