"""
Navigation function implementations for chart summarization and navigation.
"""

from typing import List, Optional, Dict, Any
from pipecat.services.llm_service import FunctionCallParams
from app.core.logger import logger
from app.agents.voice.automatic.features.charts.session_storage import get_session_storage
from app.agents.voice.automatic.services.charts.summarization_service import ChartSummarizationService
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
    generate_bar_chart,
    generate_donut_chart,
    generate_line_chart,
)
from app.agents.voice.automatic.features.charts.session_storage import register_chart_for_navigation


async def _create_visual_chart(summary_chart: Dict[str, Any], session_id: str):
    """
    Create actual visual chart using chart_tools based on AI summary data.
    
    Args:
        summary_chart: Chart data from AI summarization
        session_id: Current session ID
    """
    try:
        chart_type = summary_chart.get("type", "")
        props = summary_chart.get("props", {})
        
        # Create mock params object for chart generation functions
        class MockParams:
            def __init__(self, arguments):
                self.arguments = arguments
                self._result = None
            
            async def result_callback(self, result):
                self._result = result
                logger.info(f"Chart creation result: {result}")
        
        # Prepare arguments based on chart type
        chart_args = {
            "title": props.get("title", "Summary Chart"),
            "session_id": session_id,
            "voice_description": summary_chart.get("voiceDescription", "Chart summary"),
        }
        
        if "subtitle" in props:
            chart_args["subtitle"] = props["subtitle"]
        
        # Call appropriate chart generation function
        if chart_type == "bar-chart":
            chart_args.update({
                "categories": props.get("categories", []),
                "series_data": props.get("series", [])
            })
            mock_params = MockParams(chart_args)
            await generate_bar_chart(mock_params)
            
        elif chart_type == "donut-chart":
            chart_args.update({
                "categories": props.get("categories", []),
                "data": props.get("data", []),
                "data_type": props.get("data_type", "currency")
            })
            mock_params = MockParams(chart_args)
            await generate_donut_chart(mock_params)
            
        elif chart_type == "line-chart":
            chart_args.update({
                "categories": props.get("categories", []),
                "series_data": props.get("series", [])
            })
            mock_params = MockParams(chart_args)
            await generate_line_chart(mock_params)
            
        else:
            logger.warning(f"Unsupported chart type for visual creation: {chart_type}")
            
    except Exception as e:
        logger.error(f"Error creating visual chart: {e}")


async def summarize_charts(params: FunctionCallParams):
    """
    Summarize multiple charts into a single AI-generated chart.
    
    Args:
        params: Function call parameters containing chart_indices, summary_type, session_id
    """
    try:
        # Extract parameters
        chart_indices = params.arguments.get("chart_indices", [])
        summary_type = params.arguments.get("summary_type", "auto")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        if not chart_indices:
            await params.result_callback("Error: No chart indices provided for summarization")
            return
        
        if not session_id:
            await params.result_callback("Error: No session ID available")
            return
        
        # Get session storage
        storage = get_session_storage()
        
        # Validate chart indices exist
        chart_count = storage.get_chart_count(session_id)
        invalid_indices = [i for i in chart_indices if i < 0 or i >= chart_count]
        
        if invalid_indices:
            await params.result_callback(
                f"Error: Chart indices {invalid_indices} are invalid. Available charts: 0-{chart_count-1}"
            )
            return
        
        # Get full chart data for specified indices
        charts_data = storage.get_chart_full_data(session_id, chart_indices)
        
        if not charts_data:
            await params.result_callback("Error: Could not retrieve chart data for summarization")
            return
        
        if len(charts_data) < 2:
            await params.result_callback("Error: Need at least 2 charts to create a summary")
            return
        
        logger.info(f"Summarizing {len(charts_data)} charts for session {session_id}")
        
        # Initialize summarization service
        summarization_service = ChartSummarizationService()
        
        # Generate summary chart
        summary_chart = await summarization_service.summarize_charts(
            charts_data, session_id, summary_type
        )
        
        if not summary_chart:
            await params.result_callback("Error: Failed to generate chart summary")
            return
        
        # Create actual visual chart using chart_tools
        await _create_visual_chart(summary_chart, session_id)
        
        # Store the summary chart for navigation
        register_chart_for_navigation(session_id, summary_chart)
        
        # Get chart titles for response
        chart_titles = []
        for chart_data in charts_data:
            title = chart_data.get("props", {}).get("title", "Untitled Chart")
            chart_titles.append(title)
        
        # Success response
        response_message = f"Created summary chart combining: {', '.join(chart_titles)}. {summary_chart.get('voiceDescription', '')}"
        
        await params.result_callback(response_message)
        logger.info(f"Successfully created summary chart for session {session_id}")
        
    except Exception as e:
        error_message = f"Error creating chart summary: {str(e)}"
        logger.error(f"summarize_charts: {error_message}")
        await params.result_callback(error_message)


async def navigate_to_chart(params: FunctionCallParams):
    """
    Navigate to a specific chart by index or ID.
    
    Args:
        params: Function call parameters containing chart_index, chart_id, session_id
    """
    try:
        # Extract parameters
        chart_index = params.arguments.get("chart_index")
        chart_id = params.arguments.get("chart_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        if not session_id:
            await params.result_callback("Error: No session ID available")
            return
        
        storage = get_session_storage()
        chart_metadata = None
        
        # Try to find chart by index or ID
        if chart_index is not None:
            chart_metadata = storage.get_chart_by_index(session_id, chart_index)
            if not chart_metadata:
                chart_count = storage.get_chart_count(session_id)
                await params.result_callback(
                    f"Chart {chart_index + 1} not found. Available charts: 1-{chart_count}"
                )
                return
        elif chart_id:
            chart_metadata = storage.get_chart_by_id(session_id, chart_id)
            if not chart_metadata:
                await params.result_callback(f"Chart with ID '{chart_id}' not found")
                return
        else:
            await params.result_callback("Error: Either chart_index or chart_id must be provided")
            return
        
        # Success response
        chart_title = chart_metadata.get("title", "Untitled Chart")
        chart_number = chart_metadata.get("index", 0) + 1
        
        await params.result_callback(f"Navigating to chart {chart_number}: {chart_title}")
        logger.info(f"Navigation to chart {chart_number} requested for session {session_id}")
        
    except Exception as e:
        error_message = f"Error navigating to chart: {str(e)}"
        logger.error(f"navigate_to_chart: {error_message}")
        await params.result_callback(error_message)


async def search_charts(params: FunctionCallParams):
    """
    Search for charts by title, type, or content.
    
    Args:
        params: Function call parameters containing search_query, session_id
    """
    try:
        # Extract parameters
        search_query = params.arguments.get("search_query", "")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        if not search_query:
            await params.result_callback("Error: Search query is required")
            return
        
        if not session_id:
            await params.result_callback("Error: No session ID available")
            return
        
        storage = get_session_storage()
        matching_charts = storage.search_charts(session_id, search_query)
        
        if not matching_charts:
            await params.result_callback(f"No charts found matching '{search_query}'")
            return
        
        # Format results
        results = []
        for chart in matching_charts:
            chart_number = chart.get("index", 0) + 1
            title = chart.get("title", "Untitled Chart")
            chart_type = chart.get("chart_type", "unknown")
            results.append(f"Chart {chart_number}: {title} ({chart_type})")
        
        response = f"Found {len(matching_charts)} chart(s) matching '{search_query}':\n" + "\n".join(results)
        await params.result_callback(response)
        logger.info(f"Chart search for '{search_query}' returned {len(matching_charts)} results")
        
    except Exception as e:
        error_message = f"Error searching charts: {str(e)}"
        logger.error(f"search_charts: {error_message}")
        await params.result_callback(error_message)


async def list_charts(params: FunctionCallParams):
    """
    List all available charts with their basic information.
    
    Args:
        params: Function call parameters containing session_id
    """
    try:
        # Extract parameters
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        if not session_id:
            await params.result_callback("Error: No session ID available")
            return
        
        storage = get_session_storage()
        charts = storage.get_chart_registry(session_id)
        
        if not charts:
            await params.result_callback("No charts available in this session")
            return
        
        # Format chart list
        chart_list = []
        for chart in charts:
            chart_number = chart.get("index", 0) + 1
            title = chart.get("title", "Untitled Chart")
            chart_type = chart.get("chart_type", "unknown")
            is_summary = "📊 " if chart.get("is_summary") else ""
            chart_list.append(f"{is_summary}Chart {chart_number}: {title} ({chart_type})")
        
        response = f"Available charts ({len(charts)} total):\n" + "\n".join(chart_list)
        await params.result_callback(response)
        logger.info(f"Listed {len(charts)} charts for session {session_id}")
        
    except Exception as e:
        error_message = f"Error listing charts: {str(e)}"
        logger.error(f"list_charts: {error_message}")
        await params.result_callback(error_message)


async def get_chart_info(params: FunctionCallParams):
    """
    Get detailed information about a specific chart.
    
    Args:
        params: Function call parameters containing chart_index, chart_id, session_id
    """
    try:
        # Extract parameters
        chart_index = params.arguments.get("chart_index")
        chart_id = params.arguments.get("chart_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()
        
        if not session_id:
            await params.result_callback("Error: No session ID available")
            return
        
        storage = get_session_storage()
        chart_metadata = None
        
        # Try to find chart by index or ID
        if chart_index is not None:
            chart_metadata = storage.get_chart_by_index(session_id, chart_index)
        elif chart_id:
            chart_metadata = storage.get_chart_by_id(session_id, chart_id)
        else:
            await params.result_callback("Error: Either chart_index or chart_id must be provided")
            return
        
        if not chart_metadata:
            await params.result_callback("Chart not found")
            return
        
        # Format chart information
        chart_number = chart_metadata.get("index", 0) + 1
        title = chart_metadata.get("title", "Untitled Chart")
        subtitle = chart_metadata.get("subtitle", "")
        chart_type = chart_metadata.get("chart_type", "unknown")
        categories = chart_metadata.get("categories", [])
        series_names = chart_metadata.get("series_names", [])
        
        info_lines = [
            f"Chart {chart_number}: {title}",
            f"Type: {chart_type}"
        ]
        
        if subtitle:
            info_lines.append(f"Subtitle: {subtitle}")
        
        if categories:
            info_lines.append(f"Categories: {', '.join(categories)}")
        
        if series_names:
            info_lines.append(f"Data series: {', '.join(series_names)}")
        
        response = "\n".join(info_lines)
        await params.result_callback(response)
        logger.info(f"Provided info for chart {chart_number} in session {session_id}")
        
    except Exception as e:
        error_message = f"Error getting chart info: {str(e)}"
        logger.error(f"get_chart_info: {error_message}")
        await params.result_callback(error_message)