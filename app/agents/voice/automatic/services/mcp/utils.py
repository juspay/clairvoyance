"""
MCP utilities for intercepting and processing chart tool results.
"""

import json

from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
)
from app.agents.voice.automatic.utils.session_context import get_current_session_id


def create_chart_aware_wrapper(original_register_function):
    """
    Creates a wrapper for LLM register_function that intercepts MCP chart results.

    Args:
        original_register_function: The original LLM register_function method

    Returns:
        A wrapped register_function that handles chart data emission
    """

    def wrapped_register_function(name, function, *rf_args, **rf_kwargs):
        async def mcp_wrapper(params):
            if not hasattr(params, "result_callback"):
                await function(params)
                return

            original_callback = params.result_callback

            async def chart_aware_callback(result):
                # Parse JSON string if needed
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError:
                        pass

                # Check for chart data and register for pending emission
                if isinstance(result, dict) and result.get("success"):
                    chart_data = result.get("data")
                    if (
                        chart_data
                        and isinstance(chart_data, dict)
                        and chart_data.get("uiComponent")
                    ):
                        try:
                            session_id = get_current_session_id()
                            if session_id:
                                _register_pending_chart_emission(session_id, chart_data)
                        except Exception:
                            # Don't break MCP flow if chart registration fails
                            pass

                        await original_callback(
                            result.get(
                                "cleanVoiceDescription",
                                "Chart generated successfully",
                            )
                        )
                        return

                # For non-chart results, use original callback
                await original_callback(result)

            params.result_callback = chart_aware_callback
            await function(params)

        original_register_function(name, mcp_wrapper, *rf_args, **rf_kwargs)

    return wrapped_register_function
