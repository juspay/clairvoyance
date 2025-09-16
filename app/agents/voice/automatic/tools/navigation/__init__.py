"""
Chart navigation tools for LLM function calling.
Provides chart navigation, search, and information retrieval capabilities.
"""

from .chart_navigation import (
    navigate_to_chart,
    search_charts, 
    list_charts,
    get_chart_info
)
from .functions import summarize_charts
from . import tools

tool_functions = {
    "navigate_to_chart": navigate_to_chart,
    "search_charts": search_charts,
    "list_charts": list_charts,
    "get_chart_info": get_chart_info,
    "summarize_charts": summarize_charts
}

__all__ = ["tools", "tool_functions"]