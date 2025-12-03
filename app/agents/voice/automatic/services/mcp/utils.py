"""
MCP utilities for intercepting and processing chart tool results and HITL operations.
"""

import json
import uuid

from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
)
from app.agents.voice.automatic.features.hitl.hitl import get_hitl_manager
from app.agents.voice.automatic.features.hitl.utils import is_dangerous_operation
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core.config.static import HITL_ENABLE
from app.core.logger import logger


def create_mcp_hitl_chart_wrapper(original_register_function):
    """
    Creates a comprehensive wrapper for LLM register_function that handles both:
    1. HITL (Human-in-the-Loop) confirmation for dangerous MCP operations
    2. Chart data emission for MCP chart results

    This wrapper ensures MCP tools from lighthouse have the same security and
    functionality as local tools, providing user confirmation for dangerous
    operations and proper chart integration.

    Args:
        original_register_function: The original LLM register_function method

    Returns:
        A wrapped register_function that handles both HITL and chart functionality
    """

    def wrapped_register_function(name, function, *rf_args, **rf_kwargs):
        logger.debug(f"MCP HITL and Chart Wrapper: Registering function: {name}")
        
        # Check if this is a dangerous operation that requires HITL
        is_dangerous = HITL_ENABLE and is_dangerous_operation(name)
        
        if is_dangerous:
            logger.debug(f"MCP HITL and Chart Wrapper: {name} is a dangerous operation - adding HITL protection")

        async def mcp_hitl_chart_wrapper_function(params):
            try:
                # Get HITL manager once and reuse if needed
                hitl_manager = get_hitl_manager() if is_dangerous else None

                # Handle HITL confirmation for dangerous operations
                if is_dangerous and hasattr(params, "result_callback"):
                    arguments = getattr(params, "arguments", {})
                    tool_call_id = getattr(params, "tool_call_id", str(uuid.uuid4()))
                    result_callback = params.result_callback

                    if not result_callback:
                        logger.error(f"No result_callback found for MCP function {name}")
                        await function(params)
                        return

                    try:
                        logger.info(f"MCP HITL and Chart Wrapper: Requesting HITL confirmation for {name}")
                        confirmation_result = await hitl_manager.request_confirmation(
                            function_name=name,
                            arguments=arguments,
                            tool_call_id=tool_call_id,
                        )

                        if not confirmation_result.get("approved", False):
                            reason = confirmation_result.get("reason", "User denied operation")
                            logger.info(f"MCP HITL and Chart Wrapper: User rejected {name}. Reason: {reason}")
                            await result_callback({
                                "error": f"Operation '{name}' was not approved by user",
                                "reason": reason
                            })
                            return

                        # Update params with modified arguments if any
                        final_args = confirmation_result.get("modified_arguments", arguments)
                        if hasattr(params, "arguments"):
                            params.arguments = final_args

                        logger.info(f"MCP HITL and Chart Wrapper: User approved {name}, proceeding with execution")

                    except Exception as e:
                        logger.error(f"MCP HITL and Chart Wrapper: HITL confirmation failed for {name}: {e}")
                        await result_callback({
                            "error": f"Confirmation process failed for '{name}': {str(e)}"
                        })
                        return

                # Set up chart-aware callback wrapper
                if hasattr(params, "result_callback"):
                    original_callback = params.result_callback

                    async def enhanced_callback(result):
                        # Handle chart data emission
                        if isinstance(result, str):
                            try:
                                result = json.loads(result)
                            except json.JSONDecodeError:
                                pass

                        if isinstance(result, dict):
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
                                chart_result = clean_text or result.get("message", "Chart generated successfully")
                                
                                # Add success message for dangerous operations
                                if is_dangerous:
                                    success_msg = hitl_manager.generate_success_message(name, getattr(params, "arguments", {}))
                                    enhanced_chart_result = f"{chart_result}\n\n{success_msg}"
                                    await original_callback(enhanced_chart_result)
                                else:
                                    await original_callback(chart_result)
                                return

                        # For non-chart results, add success message if dangerous operation
                        if is_dangerous:
                            success_msg = hitl_manager.generate_success_message(name, getattr(params, "arguments", {}))
                            
                            if isinstance(result, str):
                                enhanced_result = f"{result}\n\n{success_msg}"
                            else:
                                enhanced_result = f"{str(result)}\n\n{success_msg}"
                            
                            await original_callback(enhanced_result)
                        else:
                            await original_callback(result)

                    params.result_callback = enhanced_callback

                # Execute the original MCP function
                await function(params)

            except Exception as e:
                logger.error(f"MCP HITL and Chart Wrapper: Error in wrapped function {name}: {e}")
                if hasattr(params, "result_callback") and params.result_callback:
                    await params.result_callback({
                        "error": f"MCP function execution failed: {str(e)}"
                    })
                raise

        original_register_function(name, mcp_hitl_chart_wrapper_function, *rf_args, **rf_kwargs)

    return wrapped_register_function
