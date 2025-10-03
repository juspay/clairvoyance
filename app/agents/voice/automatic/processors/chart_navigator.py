"""
ChartNavigator - Consolidated LLM-powered navigation system for chart components.
This file contains all navigation functionality in a single comprehensive module.

Features:
1. Pure LLM navigation processing (no pattern matching)
2. Azure OpenAI integration for natural language understanding
3. Chart registry and session management
4. Navigation function implementations
5. Tool schema definitions
6. WebSocket responses and frontend communication

Consolidated from multiple files for easier maintenance and reduced complexity.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import AsyncAzureOpenAI
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import Frame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
from pipecat.services.llm_service import FunctionCallParams

from app.agents.voice.automatic.features.charts.chart_tools import (
    _register_pending_chart_emission,
    generate_bar_chart,
    generate_donut_chart,
    generate_line_chart,
    get_pending_chart_emissions,
)
from app.agents.voice.automatic.features.charts.session_storage import (
    get_session_storage,
    register_chart_for_navigation,
)
from app.agents.voice.automatic.features.charts.types.ui_components import (
    UIComponentEvent,
)
from app.agents.voice.automatic.services.charts.summarization_service import (
    ChartSummarizationService,
)
from app.agents.voice.automatic.utils.session_context import (
    get_current_session_id,
)
from app.core import config
from app.core.logger import logger

# ============================================================================
# TOOL SCHEMA DEFINITIONS
# ============================================================================

summarize_charts_function = FunctionSchema(
    name="summarize_charts",
    description="Combine multiple charts into a single AI-generated summary chart",
    properties={
        "chart_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of charts to summarize (e.g., [0, 2] for charts 1 and 3)",
        },
        "summary_type": {
            "type": "string",
            "enum": ["auto", "trend", "comparison", "overview"],
            "description": "Type of summary to create",
            "default": "auto",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for chart registry lookup",
        },
    },
    required=["chart_indices"],
)

navigate_to_chart_function = FunctionSchema(
    name="navigate_to_chart",
    description="Navigate to a specific chart by index or ID",
    properties={
        "chart_index": {
            "type": "integer",
            "description": "0-based index of the chart to navigate to (e.g., 0 for chart 1)",
        },
        "chart_id": {
            "type": "string",
            "description": "Unique ID of the chart to navigate to",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for chart registry lookup",
        },
    },
    required=[],  # Either chart_index or chart_id must be provided
)

search_charts_function = FunctionSchema(
    name="search_charts",
    description="Search for charts by title, type, or content",
    properties={
        "search_query": {
            "type": "string",
            "description": "Search term to find matching charts (searches titles and categories)",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for chart registry lookup",
        },
    },
    required=["search_query"],
)

list_charts_function = FunctionSchema(
    name="list_charts",
    description="List all available charts with their basic information",
    properties={
        "session_id": {
            "type": "string",
            "description": "Session ID for chart registry lookup",
        },
    },
    required=[],
)

get_chart_info_function = FunctionSchema(
    name="get_chart_info",
    description="Get detailed information about a specific chart",
    properties={
        "chart_index": {
            "type": "integer",
            "description": "0-based index of the chart to get info for (e.g., 0 for chart 1)",
        },
        "chart_id": {
            "type": "string",
            "description": "Unique ID of the chart to get info for",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for chart registry lookup",
        },
    },
    required=[],  # Either chart_index or chart_id must be provided
)

# Standard navigation tools list
standard_tools = [
    summarize_charts_function,
    navigate_to_chart_function,
    search_charts_function,
    list_charts_function,
    get_chart_info_function,
]


# ============================================================================
# LLM NAVIGATION HANDLER
# ============================================================================


class LLMNavigationHandler:
    """
    Pure LLM-powered navigation handler.
    Processes ALL navigation commands through Azure OpenAI.
    """

    def __init__(self, llm_service=None):
        """Initialize the LLM navigation handler.

        Args:
            llm_service: Optional main pipeline LLM service to reuse.
                        If None, creates separate Azure OpenAI client.
        """
        self.llm_service = llm_service
        self._azure_client = None
        self._client_initialized = False
        self._initialization_attempted = False

    def _initialize_azure_client(self):
        """Initialize Azure OpenAI client lazily"""
        if self._initialization_attempted:
            return self._azure_client is not None

        self._initialization_attempted = True
        try:
            if hasattr(config, "AZURE_OPENAI_API_KEY") and config.AZURE_OPENAI_API_KEY:
                self._azure_client = AsyncAzureOpenAI(
                    api_key=config.AZURE_OPENAI_API_KEY,
                    api_version=getattr(
                        config, "AZURE_OPENAI_API_VERSION", "2024-02-01"
                    ),
                    azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                )
                self._client_initialized = True
                logger.info("LLMNavigationHandler: Azure OpenAI client initialized")
                return True
            else:
                logger.warning("LLMNavigationHandler: Azure OpenAI not configured")
                return False
        except Exception as e:
            logger.error(
                f"LLMNavigationHandler: Failed to initialize Azure client: {e}"
            )
            return False

    async def _call_main_llm_service(self, prompt: str):
        """
        Call navigation through main pipeline LLM service.

        Args:
            prompt: The navigation prompt to send

        Returns:
            Response object compatible with Azure OpenAI format
        """
        try:
            # Create messages for the main LLM service
            messages = [
                {"role": "system", "content": self._get_system_prompt()},
                {"role": "user", "content": prompt},
            ]

            # Use the main LLM service (which may be wrapped)
            if hasattr(self.llm_service, "service"):
                # If it's wrapped (LLMServiceWrapper), get the underlying service
                llm = self.llm_service.service
            else:
                # Direct service
                llm = self.llm_service

            # Call the LLM service using correct attributes
            logger.info(
                f"LLMNavigationHandler: Calling main LLM with model: {llm.model_name}"
            )
            response = await llm._client.chat.completions.create(
                model=llm.model_name,  # Corrected: use model_name instead of _model
                messages=messages,
                temperature=0.1,
                max_tokens=500,
            )

            logger.info("LLMNavigationHandler: Successfully called main LLM service")
            return response

        except Exception as e:
            logger.error(f"LLMNavigationHandler: Error calling main LLM service: {e}")
            raise

    async def process_navigation_command(
        self,
        text: str,
        available_charts: List[Dict[str, Any]] = None,
        session_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Process navigation command through LLM.

        Args:
            text: User voice input
            available_charts: List of available charts for context
            session_id: Current session ID

        Returns:
            Dict with navigation action or None if not navigation
        """
        try:
            # Get available charts if not provided
            if available_charts is None and session_id:
                storage = get_session_storage()
                available_charts = storage.get_chart_registry(session_id)

            # Create chart context for LLM
            chart_context = self._format_chart_context(available_charts or [])

            # Create navigation prompt
            prompt = self._create_navigation_prompt(text, chart_context)

            # Try to use main pipeline LLM service first
            if self.llm_service:
                logger.info("LLMNavigationHandler: Using main pipeline LLM service")
                response = await self._call_main_llm_service(prompt)
            else:
                # Fallback to separate Azure client
                logger.info("LLMNavigationHandler: Using separate Azure client")
                if not self._initialize_azure_client():
                    logger.warning("LLMNavigationHandler: Azure client not available")
                    return None

                response = await self._azure_client.chat.completions.create(
                    model=getattr(config, "AZURE_OPENAI_MODEL_NAME", "gpt-4o"),
                    messages=[
                        {"role": "system", "content": self._get_system_prompt()},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=500,
                )

            # Parse LLM response
            result = self._parse_llm_response(response.choices[0].message.content)

            if result:
                logger.info(
                    f"LLMNavigationHandler: Processed '{text}' → {result.get('type', 'unknown')}"
                )
            else:
                logger.debug(
                    f"LLMNavigationHandler: '{text}' not recognized as navigation"
                )

            return result

        except Exception as e:
            logger.error(f"LLMNavigationHandler: Error processing command: {e}")
            return None

    def _get_system_prompt(self) -> str:
        """Get the system prompt for navigation LLM"""
        return """You are a chart navigation assistant. Analyze user voice commands and determine if they are navigation requests.

NAVIGATION TYPES:
1. Direction navigation: "next", "previous", "back", "forward"
2. Specific chart: "go to chart 3", "show chart 2", "chart number 4"
3. Multi-chart display: "show chart 1, 2 and 3 together", "display charts 1, 2, 3"
4. Search navigation: "show sales chart", "revenue chart", "payment errors"
5. Chart enumeration: "how many charts", "list charts"
6. Chart summarization: "combine charts 1 and 3", "summarize these charts"

RESPONSE FORMAT (JSON only):
- Direction: {"type": "navigate_direction", "direction": "next|previous"}
- Specific: {"type": "navigate_to_chart", "target_chart_index": 2, "target_chart_id": "chart_id"}
- Multi-display: {"type": "display_multiple_charts", "chart_indices": [0, 1, 2]}
- Search: {"type": "search_charts", "query": "sales", "matching_charts": [...]}
- List: {"type": "list_charts"}
- Summarize: {"type": "summarize_charts", "chart_indices": [0, 2], "summary_type": "auto"}
- Not navigation: {"type": "not_navigation"}

Be flexible with natural language - "go back" = "previous", "next one" = "next", etc.

EXAMPLES:
- "show chart 1, 2 and 3 together" → {"type": "display_multiple_charts", "chart_indices": [0, 1, 2]}
- "display charts 1, 2, 3" → {"type": "display_multiple_charts", "chart_indices": [0, 1, 2]}
- "show me charts 2 and 4 side by side" → {"type": "display_multiple_charts", "chart_indices": [1, 3]}
- "combine chart 1 and 3" → {"type": "summarize_charts", "chart_indices": [0, 2], "summary_type": "auto"}

Note: Use 0-based indices (chart 1 = index 0, chart 2 = index 1, etc.)"""

    def _format_chart_context(self, charts: List[Dict[str, Any]]) -> str:
        """Format available charts for LLM context"""
        if not charts:
            return "No charts available."

        chart_list = []
        for i, chart in enumerate(charts):
            title = chart.get("title", "Untitled Chart")
            chart_type = chart.get("type", "unknown")
            chart_id = chart.get("id", "")
            chart_list.append(
                f"Chart {i+1} (index {i}): {title} [{chart_type}] (ID: {chart_id})"
            )

        return f"Available charts ({len(charts)} total):\n" + "\n".join(chart_list)

    def _create_navigation_prompt(self, text: str, chart_context: str) -> str:
        """Create the navigation prompt for LLM"""
        return f"""User voice command: "{text}"

{chart_context}

Is this a chart navigation command? Don't be a strict judge as TTS can do mistake. Try to map the user query to some chart command. Only if it doesn't make any sense then say not_navigation. If yes, provide the appropriate JSON response. If no, return {{"type": "not_navigation"}}."""

    def _parse_llm_response(self, response: str) -> Optional[Dict[str, Any]]:
        """Parse LLM JSON response"""
        try:
            # Clean response and extract JSON
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            # Parse JSON
            result = json.loads(response)

            # Validate response type
            if result.get("type") == "not_navigation":
                return None

            # Add missing fields for specific navigation types
            if result.get("type") == "search_charts":
                # Perform actual search if needed
                query = result.get("query", "")
                if query and hasattr(self, "_session_id"):
                    storage = get_session_storage()
                    matching_charts = storage.search_charts(self._session_id, query)
                    result["matching_charts"] = matching_charts

            return result

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"LLMNavigationHandler: Failed to parse LLM response: {e}")
            logger.error(f"LLMNavigationHandler: Raw response: {response}")
            return None

    async def process_navigation_command_simple(
        self, text: str, session_id: str = None
    ) -> Optional[Dict[str, Any]]:
        """
        Simplified method that gets available charts automatically.

        Args:
            text: User voice input
            session_id: Current session ID

        Returns:
            Dict with navigation action or None if not navigation
        """
        # Get available charts
        available_charts = []
        if session_id:
            storage = get_session_storage()
            available_charts = storage.get_chart_registry(session_id)

        return await self.process_navigation_command(text, available_charts, session_id)


# ============================================================================
# NAVIGATION FUNCTION IMPLEMENTATIONS
# ============================================================================


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
            chart_args.update(
                {
                    "categories": props.get("categories", []),
                    "series_data": props.get("series", []),
                }
            )
            mock_params = MockParams(chart_args)
            await generate_bar_chart(mock_params)

        elif chart_type == "donut-chart":
            chart_args.update(
                {
                    "categories": props.get("categories", []),
                    "data": props.get("data", []),
                    "data_type": props.get("data_type", "currency"),
                }
            )
            mock_params = MockParams(chart_args)
            await generate_donut_chart(mock_params)

        elif chart_type == "line-chart":
            chart_args.update(
                {
                    "categories": props.get("categories", []),
                    "series_data": props.get("series", []),
                }
            )
            mock_params = MockParams(chart_args)
            await generate_line_chart(mock_params)

        else:
            logger.warning(f"Unsupported chart type for visual creation: {chart_type}")

    except Exception as e:
        logger.error(f"Error creating visual chart: {e}")


async def summarize_charts(params: FunctionCallParams, llm_service=None):
    """
    Summarize multiple charts into a single AI-generated chart.

    Args:
        params: Function call parameters containing chart_indices, summary_type, session_id
        llm_service: Optional LLM service to reuse. If None, creates separate Azure client.
    """
    try:
        # Extract parameters
        chart_indices = params.arguments.get("chart_indices", [])
        summary_type = params.arguments.get("summary_type", "auto")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not chart_indices:
            await params.result_callback(
                "Error: No chart indices provided for summarization"
            )
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

        # Get full chart data for specified indices - we need to restore this
        charts_data = []
        for index in chart_indices:
            chart_metadata = storage.get_chart_by_index(session_id, index)
            if chart_metadata:
                logger.info(
                    f"🔍 Chart {index} data retrieved: {chart_metadata.get('props', {}).get('title', 'No title')}"
                )
                logger.info(
                    f"🔍 Chart {index} categories: {chart_metadata.get('props', {}).get('categories', [])}"
                )
                logger.info(
                    f"🔍 Chart {index} series count: {len(chart_metadata.get('props', {}).get('series', []))}"
                )
                charts_data.append(chart_metadata)

        if not charts_data:
            await params.result_callback(
                "Error: Could not retrieve chart data for summarization"
            )
            return

        if len(charts_data) < 2:
            await params.result_callback(
                "Error: Need at least 2 charts to create a summary"
            )
            return

        logger.info(f"Summarizing {len(charts_data)} charts for session {session_id}")

        # Initialize summarization service with LLM service if available
        summarization_service = ChartSummarizationService(llm_service=llm_service)

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
            title = chart_data.get("title", "Untitled Chart")
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
            await params.result_callback(
                "Error: Either chart_index or chart_id must be provided"
            )
            return

        # Success response
        chart_title = chart_metadata.get("title", "Untitled Chart")
        chart_number = chart_metadata.get("index", 0) + 1

        await params.result_callback(
            f"Navigating to chart {chart_number}: {chart_title}"
        )
        logger.info(
            f"Navigation to chart {chart_number} requested for session {session_id}"
        )

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
        # Simple search through chart registry
        all_charts = storage.get_chart_registry(session_id)
        matching_charts = []

        query_lower = search_query.lower()
        for chart in all_charts:
            # Search in title
            if query_lower in chart.get("title", "").lower():
                matching_charts.append(chart)
                continue
            # Search in categories
            categories = chart.get("categories", [])
            if any(query_lower in str(cat).lower() for cat in categories):
                matching_charts.append(chart)
                continue

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

        response = (
            f"Found {len(matching_charts)} chart(s) matching '{search_query}':\n"
            + "\n".join(results)
        )
        await params.result_callback(response)
        logger.info(
            f"Chart search for '{search_query}' returned {len(matching_charts)} results"
        )

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
            chart_list.append(
                f"{is_summary}Chart {chart_number}: {title} ({chart_type})"
            )

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
            await params.result_callback(
                "Error: Either chart_index or chart_id must be provided"
            )
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

        info_lines = [f"Chart {chart_number}: {title}", f"Type: {chart_type}"]

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


# Wrapper for tool registration (maintains backward compatibility)
async def summarize_charts_tool(params: FunctionCallParams):
    """Tool-compatible wrapper for summarize_charts function"""
    return await summarize_charts(params, llm_service=None)


# Export tool functions for LLM integration
tool_functions = {
    "summarize_charts": summarize_charts_tool,
    "navigate_to_chart": navigate_to_chart,
    "search_charts": search_charts,
    "list_charts": list_charts,
    "get_chart_info": get_chart_info,
}


# ============================================================================
# MAIN CHART NAVIGATOR CLASS
# ============================================================================


class ChartNavigator(FrameProcessor):
    """
    Pure LLM chart navigation processor - no pattern matching.

    Features:
    1. LLM processes ALL navigation commands (simple and complex)
    2. WebSocket responses: Send navigation results to frontend

    Processing Flow:
    1. Transcription → LLM navigation handler → navigation result
    2. WebSocket emission for frontend navigation
    """

    def __init__(self, name: str = "ChartNavigator"):
        """Initialize the chart navigator"""
        super().__init__(name=name)
        self._minimap_active = False
        self._navigation_handler = None  # Will be set externally
        self._rtvi_processor = None
        logger.info("ChartNavigator initialized with pure LLM processing")

    def set_minimap_active(self, active: bool) -> None:
        """Update the minimap active state"""
        if self._minimap_active != active:
            logger.info(
                f"🗺️ ChartNavigator: MINIMAP STATE CHANGE: {self._minimap_active} → {active} ({'ACTIVE' if active else 'INACTIVE'})"
            )
            self._minimap_active = active
        else:
            logger.info(f"🗺️ ChartNavigator: Minimap state unchanged: {active}")

    def is_minimap_active(self) -> bool:
        """Get current minimap state"""
        return self._minimap_active

    def set_navigation_handler(self, handler: LLMNavigationHandler) -> None:
        """Set the navigation handler for processing commands"""
        self._navigation_handler = handler

    def set_rtvi_processor(self, rtvi_processor) -> None:
        """Set the RTVI processor for sending chart navigation"""
        self._rtvi_processor = rtvi_processor

    async def _process_navigation_command(self, text: str) -> bool:
        """
        Process navigation command through LLM.

        Args:
            text: The transcribed navigation command

        Returns:
            True if command was handled, False otherwise
        """
        if not self._navigation_handler:
            logger.debug(
                "ChartNavigator: No navigation handler available - skipping LLM processing"
            )
            return False

        try:
            # Let LLM decide if this is navigation and how to handle it
            from app.agents.voice.automatic.utils.session_context import (
                get_current_session_id,
            )

            session_id = get_current_session_id()
            if not session_id:
                logger.warning(
                    "ChartNavigator: No session ID available - cannot process navigation"
                )
                return False

            result = await self._navigation_handler.process_navigation_command_simple(
                text, session_id
            )

            if result and isinstance(result, dict):
                # Handle different result types
                result_type = result.get("type")

                try:
                    if result_type == "summarize_charts":
                        # For summarization, trigger the LLM function directly
                        await self._handle_chart_summarization(result, session_id)
                        return True
                    elif result_type == "display_multiple_charts":
                        # For multi-chart display, emit multiple UI components
                        await self._handle_multi_chart_display(result, session_id)
                        return True
                    elif result_type == "not_navigation":
                        return False
                    else:
                        # Convert LLM result to simple chart navigation
                        navigation_data = self._convert_llm_result_to_navigation(result)
                        if navigation_data:
                            await self._send_chart_navigation(navigation_data)
                            return True
                        else:
                            logger.warning(
                                "ChartNavigator: Failed to convert LLM result to navigation data"
                            )
                except Exception as processing_error:
                    logger.error(
                        f"ChartNavigator: Error processing navigation result: {processing_error}"
                    )
                    return False

            return False

        except Exception as e:
            logger.error(
                f"ChartNavigator: Error processing navigation command '{text}': {e}",
                exc_info=True,
            )
            return False

    def _convert_llm_result_to_navigation(
        self, llm_result: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Convert LLM navigation result to simple chart navigation data"""
        try:
            result_type = llm_result.get("type")

            if result_type == "navigate_to_chart":
                return {
                    "action": "navigate_to_chart",
                    "chart_index": llm_result.get("target_chart_index"),
                    "chart_id": llm_result.get("target_chart_id"),
                    "lastChart": llm_result.get("lastChart", False),
                }
            elif result_type == "navigate_direction":
                return {
                    "action": "navigate",
                    "direction": llm_result.get("direction"),
                    "lastChart": False,
                }
            elif result_type == "search_charts":
                # For search, navigate to first result if available
                if llm_result.get("matching_charts"):
                    first_match = llm_result["matching_charts"][0]
                    return {
                        "action": "navigate_to_chart",
                        "chart_index": first_match.get("index"),
                        "chart_id": first_match.get("id"),
                        "lastChart": False,
                    }

            return None
        except Exception as e:
            logger.error(f"ChartNavigator: Error converting LLM result: {e}")
            return None

    async def _handle_chart_summarization(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle chart summarization by calling the LLM function directly.

        Args:
            llm_result: The LLM navigation result containing summarization parameters
            session_id: Current session ID
        """
        try:
            # Create function call parameters from LLM result
            chart_indices = llm_result.get("chart_indices", [])
            summary_type = llm_result.get("summary_type", "auto")

            # Create a mock FunctionCallParams object
            class MockParams:
                def __init__(self, arguments):
                    self.arguments = arguments
                    self._result = None

                async def result_callback(self, result):
                    self._result = result
                    logger.info(f"ChartNavigator: Summarization result: {result}")

            params = MockParams(
                {
                    "chart_indices": chart_indices,
                    "summary_type": summary_type,
                    "session_id": session_id,
                }
            )

            # Call the summarization function with LLM service
            llm_service = None
            if self._navigation_handler and hasattr(
                self._navigation_handler, "llm_service"
            ):
                llm_service = self._navigation_handler.llm_service
            await summarize_charts(params, llm_service=llm_service)

            # Emit any pending chart components to frontend
            pending_charts = get_pending_chart_emissions(session_id)
            if pending_charts and self._rtvi_processor:
                for chart_data in pending_charts:
                    await self._rtvi_processor.push_frame(
                        RTVIServerMessageFrame(
                            data={"type": "ui-component", "payload": chart_data}
                        )
                    )
                    logger.info(
                        f"ChartNavigator: Emitted chart component: {chart_data.get('id', 'unknown')}"
                    )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling chart summarization: {e}")

    async def _handle_multi_chart_display(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle multi-chart display by emitting multiple UI components.

        Args:
            llm_result: The LLM navigation result containing chart indices
            session_id: Current session ID
        """
        try:
            # Get chart indices from LLM result
            chart_indices = llm_result.get("chart_indices", [])

            if not chart_indices:
                logger.warning(
                    "ChartNavigator: No chart indices provided for multi-chart display"
                )
                return

            # Get the requested charts from session storage
            storage = get_session_storage()
            charts = storage.get_charts_by_indices(session_id, chart_indices)

            if not charts:
                logger.warning(
                    f"ChartNavigator: No charts found for indices {chart_indices}"
                )
                return

            # Convert chart metadata back to UI components and emit each one
            ui_components = []
            for chart_metadata in charts:
                # Reconstruct complete UI component from stored chart data
                chart_props = chart_metadata.get("props", {})
                chart_type = chart_metadata.get("type")

                # Base UI component structure
                ui_component = {
                    "id": chart_metadata.get("id"),
                    "type": chart_type,
                    "props": {
                        "chartId": chart_metadata.get("id"),
                        # Copy ALL original props to preserve chart-specific data
                        **chart_props,
                    },
                    "metadata": {
                        "index": chart_metadata.get("index"),
                        "generatedAt": chart_metadata.get("created_at"),
                        "chartType": "chart",
                    },
                    "uiComponent": True,
                }
                ui_components.append(ui_component)

            # Emit all UI components as a single array via RTVI
            if self._rtvi_processor:
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-components",
                            "payload": {
                                "displayMode": "multi-chart",
                                "requestedIndices": chart_indices,
                                "totalComponents": len(ui_components),
                                "components": ui_components,
                            },
                        }
                    )
                )

                chart_titles = [
                    comp.get("props", {}).get("title", "Unknown")
                    for comp in ui_components
                ]
                logger.info(
                    f"ChartNavigator: Emitted multi-chart display with {len(ui_components)} charts: {', '.join(chart_titles)}"
                )
            else:
                logger.warning(
                    "ChartNavigator: No RTVI processor available for multi-chart display"
                )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling multi-chart display: {e}")

    async def _send_chart_navigation(self, navigation_data: Dict[str, Any]) -> None:
        """
        Send chart navigation to frontend via RTVI.

        Args:
            navigation_data: Navigation data (which chart to show)
        """
        try:
            # Send via RTVI if processor is available
            if self._rtvi_processor:
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": "chart-navigation", "payload": navigation_data}
                    )
                )
                logger.info(
                    f"ChartNavigator: Sent navigation via RTVI: {navigation_data.get('action', 'unknown')}"
                )
            else:
                # Fallback: log for debugging
                logger.info(
                    f"ChartNavigator: Chart navigation (no RTVI): {json.dumps(navigation_data, indent=2)}"
                )
        except Exception as e:
            logger.error(f"ChartNavigator: Error sending chart navigation: {e}")

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """
        Process frames and handle navigation commands when minimap is active.
        When minimap is inactive, be completely transparent.

        Args:
            frame: The frame to process
            direction: The direction of frame flow
        """
        # Call super().process_frame first (standard pattern)
        await super().process_frame(frame, direction)

        try:
            # When minimap is inactive, be completely transparent - just push frame through
            if not self._minimap_active:
                await self.push_frame(frame, direction)
                return

            # Minimap is active - navigation-only mode
            elif self._minimap_active and isinstance(frame, TranscriptionFrame):
                text = frame.text
                logger.info(
                    f"ChartNavigator: Processing TranscriptionFrame with text: '{text}' in minimap mode"
                )

                # Process navigation command
                handled = await self._process_navigation_command(text)

                if handled:
                    logger.info(
                        f"ChartNavigator: Navigation handled for '{text}', BLOCKING frame from LLM (no bot response)"
                    )
                    return  # ✅ STOP HERE - Block frame from reaching LLM
                else:
                    logger.info(
                        f"ChartNavigator: Not a navigation command '{text}', passing through to LLM for normal conversation"
                    )
                    return  # Allow normal conversation

            # Non-transcription frames during minimap - allow through (system frames, etc.)
            else:
                return

        except Exception as e:
            logger.error(f"ChartNavigator: Frame processing failed: {e}")
            # Still try to pass frame through to avoid breaking pipeline
            try:
                await self.push_frame(frame, direction)
            except Exception as fallback_error:
                logger.error(
                    f"ChartNavigator: Fallback frame processing also failed: {fallback_error}"
                )
                raise
