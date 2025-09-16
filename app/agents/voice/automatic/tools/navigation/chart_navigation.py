"""
Chart navigation tools for LLM function calling.
Allows the LLM to navigate between charts, search charts, and get chart information.
"""

from typing import Dict, Any, List, Optional
from app.core.logger import logger
from app.agents.voice.automatic.features.charts.session_storage import get_session_storage
from app.agents.voice.automatic.utils.session_context import get_current_session_id


async def navigate_to_chart(params) -> None:
    """Navigate to a specific chart by index or ID."""
    try:
        # Extract parameters from LLM call
        chart_index = params.arguments.get("chart_index")
        chart_id = params.arguments.get("chart_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        storage = get_session_storage()
        target_chart = None
        
        # Try to find chart by index first, then by ID
        if chart_index is not None:
            target_chart = storage.get_chart_by_index(session_id, int(chart_index))
        elif chart_id:
            target_chart = storage.get_chart_by_id(session_id, chart_id)
        
        if target_chart:
            # Send navigation response (this would trigger frontend navigation)
            navigation_response = {
                "type": "navigate_to_chart",
                "target_chart": target_chart,
                "chart_index": target_chart["index"],
                "chart_id": target_chart["id"],
                "message": f"Navigating to chart {target_chart['index'] + 1}: {target_chart['title']}"
            }
            
            logger.info(f"[{session_id}] Navigation tool: Navigating to chart '{target_chart['title']}'")
            
            # In a real implementation, this would emit to WebSocket
            # For now, we'll just return the success message
            await params.result_callback(navigation_response["message"])
        else:
            error_msg = f"Chart not found (index: {chart_index}, id: {chart_id})"
            logger.warning(f"[{session_id}] Navigation tool: {error_msg}")
            await params.result_callback(f"Error: {error_msg}")
            
    except Exception as e:
        error_message = str(e)
        logger.error(f"[{session_id}] Error in navigate_to_chart: {error_message}")
        await params.result_callback(f"Error navigating to chart: {error_message}")


async def search_charts(params) -> None:
    """Search for charts by title, type, or content."""
    try:
        # Extract parameters from LLM call
        search_query = params.arguments.get("search_query", "")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        storage = get_session_storage()
        matching_charts = storage.search_charts(session_id, search_query)
        
        if matching_charts:
            # Format the results
            results = []
            for chart in matching_charts:
                chart_info = f"Chart {chart['index'] + 1}: {chart['title']}"
                if chart.get('type'):
                    chart_info += f" ({chart['type']})"
                results.append(chart_info)
            
            result_message = f"Found {len(matching_charts)} chart(s) matching '{search_query}':\\n" + "\\n".join(results)
            logger.info(f"[{session_id}] Search tool: Found {len(matching_charts)} charts for query '{search_query}'")
            await params.result_callback(result_message)
        else:
            result_message = f"No charts found matching '{search_query}'"
            logger.info(f"[{session_id}] Search tool: No charts found for query '{search_query}'")
            await params.result_callback(result_message)
            
    except Exception as e:
        error_message = str(e)
        logger.error(f"[{session_id}] Error in search_charts: {error_message}")
        await params.result_callback(f"Error searching charts: {error_message}")


async def list_charts(params) -> None:
    """List all available charts with their information."""
    try:
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        storage = get_session_storage()
        charts = storage.get_chart_registry(session_id)
        chart_count = storage.get_chart_count(session_id)
        
        if charts:
            # Format the chart list
            chart_list = []
            for chart in charts:
                chart_info = f"Chart {chart['index'] + 1}: {chart['title']}"
                if chart.get('type'):
                    chart_info += f" ({chart['type']})"
                if chart.get('subtitle'):
                    chart_info += f" - {chart['subtitle']}"
                chart_list.append(chart_info)
            
            result_message = f"Available charts ({chart_count} total):\\n" + "\\n".join(chart_list)
            logger.info(f"[{session_id}] List tool: Listed {chart_count} charts")
            await params.result_callback(result_message)
        else:
            result_message = "No charts are currently available"
            logger.info(f"[{session_id}] List tool: No charts available")
            await params.result_callback(result_message)
            
    except Exception as e:
        error_message = str(e)
        logger.error(f"[{session_id}] Error in list_charts: {error_message}")
        await params.result_callback(f"Error listing charts: {error_message}")


async def get_chart_info(params) -> None:
    """Get detailed information about a specific chart."""
    try:
        # Extract parameters from LLM call
        chart_index = params.arguments.get("chart_index")
        chart_id = params.arguments.get("chart_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        storage = get_session_storage()
        target_chart = None
        
        # Try to find chart by index first, then by ID
        if chart_index is not None:
            target_chart = storage.get_chart_by_index(session_id, int(chart_index))
        elif chart_id:
            target_chart = storage.get_chart_by_id(session_id, chart_id)
        
        if target_chart:
            # Format detailed chart information
            info_lines = [
                f"Chart {target_chart['index'] + 1}: {target_chart['title']}",
                f"Type: {target_chart.get('type', 'Unknown')}",
                f"ID: {target_chart.get('id', 'N/A')}"
            ]
            
            if target_chart.get('subtitle'):
                info_lines.append(f"Subtitle: {target_chart['subtitle']}")
            
            if target_chart.get('categories'):
                categories = target_chart['categories']
                if len(categories) <= 5:
                    info_lines.append(f"Categories: {', '.join(categories)}")
                else:
                    info_lines.append(f"Categories: {', '.join(categories[:5])} and {len(categories) - 5} more")
            
            if target_chart.get('series_names'):
                series = [name for name in target_chart['series_names'] if name]
                if series:
                    info_lines.append(f"Data series: {', '.join(series)}")
            
            info_lines.append(f"Created: {target_chart.get('created_at', 'Unknown')}")
            
            result_message = "\\n".join(info_lines)
            logger.info(f"[{session_id}] Chart info tool: Retrieved info for chart '{target_chart['title']}'")
            await params.result_callback(result_message)
        else:
            error_msg = f"Chart not found (index: {chart_index}, id: {chart_id})"
            logger.warning(f"[{session_id}] Chart info tool: {error_msg}")
            await params.result_callback(f"Error: {error_msg}")
            
    except Exception as e:
        error_message = str(e)
        logger.error(f"[{session_id}] Error in get_chart_info: {error_message}")
        await params.result_callback(f"Error getting chart info: {error_message}")