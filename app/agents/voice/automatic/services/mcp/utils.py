"""
MCP utilities for intercepting and processing chart tool results.
"""

import json

from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
)
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core.logger import logger


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
                try:
                    await function(params)
                except Exception as e:
                    logger.error(
                        f"Tool Error: [Tool name'{name}'] Unexpected error during execution: {e}"
                    )
                return

            original_callback = params.result_callback

            async def chart_aware_callback(result):
                # Parse JSON string if needed
                if isinstance(result, str):
                    try:
                        result = json.loads(result)
                    except json.JSONDecodeError as e:
                        logger.error(
                            f"Tool Error: [Tool '{name}'] Failed to parse JSON result: {e}"
                        )
                        # Continue with string result

                # Check for error in result
                if isinstance(result, dict):
                    if "error" in result:
                        logger.error(
                            f"Tool Error: [Tool '{name}'] Tool returned error: {result.get('error')}"
                        )

                    comments = result.get("comments", {})
                    chart_data = comments.get("data")
                    if (
                        chart_data
                        and isinstance(chart_data, dict)
                        and chart_data.get("uiComponent")
                    ):
                        try:
                            session_id = get_current_session_id()
                            if session_id:
                                _register_pending_chart_emission(session_id, chart_data)

                        except Exception as e:
                            # Don't break MCP flow if chart registration fails
                            logger.warning(
                                f"Failed to register pending chart emission: {e}",
                                exc_info=True,
                            )

                        clean_text = chart_data.get("metadata", {}).get(
                            "cleanVoiceDescription"
                        )
                        await original_callback(
                            clean_text
                            or result.get("message", "Chart generated successfully")
                        )
                        return

                # For non-chart results, use original callback
                try:
                    await original_callback(result)
                except Exception as e:
                    logger.error(
                        f"Tool Error: [Tool '{name}'] Failed to execute result callback: {e}"
                    )

            params.result_callback = chart_aware_callback

            try:
                await function(params)
            except Exception as e:
                logger.error(
                    f"Tool Error: [Tool '{name}'] Unexpected error during execution: {e}"
                )

        original_register_function(name, mcp_wrapper, *rf_args, **rf_kwargs)

    return wrapped_register_function
