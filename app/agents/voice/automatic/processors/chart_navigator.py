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

# Image navigation functions
navigate_to_image_function = FunctionSchema(
    name="navigate_to_image",
    description="Navigate to a specific image by index or ID",
    properties={
        "image_index": {
            "type": "integer",
            "description": "0-based index of the image to navigate to (e.g., 0 for image 1)",
        },
        "image_id": {
            "type": "string",
            "description": "Unique ID of the image to navigate to",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=[],  # Either image_index or image_id must be provided
)

list_images_function = FunctionSchema(
    name="list_images",
    description="List all available images with their basic information",
    properties={
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=[],
)

get_image_info_function = FunctionSchema(
    name="get_image_info",
    description="Get detailed information about a specific image",
    properties={
        "image_index": {
            "type": "integer",
            "description": "0-based index of the image to get info for (e.g., 0 for image 1)",
        },
        "image_id": {
            "type": "string",
            "description": "Unique ID of the image to get info for",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=[],  # Either image_index or image_id must be provided
)

display_multiple_images_function = FunctionSchema(
    name="display_multiple_images",
    description="Display multiple images simultaneously",
    properties={
        "image_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of images to display (e.g., [0, 2] for images 1 and 3)",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=["image_indices"],
)

# Mixed component functions
display_mixed_components_function = FunctionSchema(
    name="display_mixed_components",
    description="Display charts and images together in a unified view",
    properties={
        "chart_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of charts to include",
        },
        "image_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of images to include",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for component registry lookup",
        },
    },
    required=[],  # At least one of chart_indices or image_indices should be provided
)

list_components_function = FunctionSchema(
    name="list_components",
    description="List all available components (charts and images) with their information",
    properties={
        "session_id": {
            "type": "string",
            "description": "Session ID for component registry lookup",
        },
    },
    required=[],
)

search_components_function = FunctionSchema(
    name="search_components",
    description="Search for components (charts and images) by title, type, or content",
    properties={
        "search_query": {
            "type": "string",
            "description": "Search term to find matching components (searches titles and descriptions)",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for component registry lookup",
        },
    },
    required=["search_query"],
)

# Email function
email_images_function = FunctionSchema(
    name="email_images",
    description="Send specified images via email to the configured recipient",
    properties={
        "image_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "0-based indices of images to email (e.g., [0, 1] for images 1 and 2)",
        },
        "subject": {
            "type": "string",
            "description": "Optional custom email subject line",
        },
        "message": {
            "type": "string",
            "description": "Optional custom message to include in email body",
        },
        "session_id": {
            "type": "string",
            "description": "Session ID for image registry lookup",
        },
    },
    required=["image_indices"],
)

# Standard navigation tools list (now includes image navigation and email)
standard_tools = [
    # Chart-specific tools
    summarize_charts_function,
    navigate_to_chart_function,
    search_charts_function,
    list_charts_function,
    get_chart_info_function,
    # Image-specific tools
    navigate_to_image_function,
    list_images_function,
    get_image_info_function,
    display_multiple_images_function,
    # Mixed component tools
    display_mixed_components_function,
    list_components_function,
    search_components_function,
    # Email tools
    email_images_function,
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
        available_components: List[Dict[str, Any]] = None,
        session_id: str = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Process navigation command through LLM.

        Args:
            text: User voice input
            available_components: List of available components (charts + images) for context
            session_id: Current session ID

        Returns:
            Dict with navigation action or None if not navigation
        """
        try:
            # Get available components if not provided
            if available_components is None and session_id:
                storage = get_session_storage()
                available_components = storage.get_component_registry(session_id)

            # Create component context for LLM
            component_context = self._format_component_context(
                available_components or []
            )

            # Create navigation prompt
            prompt = self._create_navigation_prompt(text, component_context)

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
        return """You are a unified navigation assistant for charts and images. Analyze user voice commands and determine if they are navigation requests.

NAVIGATION TYPES:
1. Direction navigation: "next", "previous", "back", "forward"
2. Specific component: "go to chart 3", "show image 2", "component number 4"
3. Multi-component display: "show chart 1 and image 2 together", "display images 1, 2, 3", "charts 1, 2 with image 3"
4. Search navigation: "show sales chart", "revenue chart", "advertisement image", "logo image"
5. Component enumeration: "how many charts", "list images", "show all components"
6. Chart summarization: "combine charts 1 and 3", "summarize these charts"
7. Image navigation: "show first image", "next advertisement", "previous image"
8. Mixed display: "show chart 1 with image 2", "display all charts and images"
9. Email images: "email me first and second image", "send images 1, 2 via email", "email these images"

RESPONSE FORMAT (JSON only):
- Direction: {"type": "navigate_direction", "direction": "next|previous"}
- Specific chart: {"type": "navigate_to_chart", "target_chart_index": 2, "target_chart_id": "chart_id"}
- Specific image: {"type": "navigate_to_image", "target_image_index": 1, "target_image_id": "image_id"}
- Specific component: {"type": "navigate_to_component", "target_global_index": 3, "target_component_id": "comp_id"}
- Multi-chart display: {"type": "display_multiple_charts", "chart_indices": [0, 1, 2]}
- Multi-image display: {"type": "display_multiple_images", "image_indices": [0, 1]}
- Mixed display: {"type": "display_mixed_components", "chart_indices": [0, 2], "image_indices": [1, 3]}
- Multi-component display: {"type": "display_multiple_components", "global_indices": [0, 1, 2]}
- Search: {"type": "search_components", "query": "sales", "matching_components": [...]}
- List charts: {"type": "list_charts"}
- List images: {"type": "list_images"}
- List all: {"type": "list_components"}
- Summarize: {"type": "summarize_charts", "chart_indices": [0, 2], "summary_type": "auto"}
- Email images: {"type": "email_images", "image_indices": [0, 1], "subject": "optional", "message": "optional"}
- Not navigation: {"type": "not_navigation"}

Be flexible with natural language - "go back" = "previous", "next one" = "next", "advertisement" = "image", etc.

EXAMPLES:
- "show chart 1, 2 and 3 together" → {"type": "display_multiple_charts", "chart_indices": [0, 1, 2]}
- "display images 1 and 2" → {"type": "display_multiple_images", "image_indices": [0, 1]}
- "show chart 1 with image 2" → {"type": "display_mixed_components", "chart_indices": [0], "image_indices": [1]}
- "show all components" → {"type": "list_components"}
- "next image" → {"type": "navigate_direction", "direction": "next", "component_type": "image"}
- "go to advertisement 2" → {"type": "navigate_to_image", "target_image_index": 1}
- "email me first and second image" → {"type": "email_images", "image_indices": [0, 1]}
- "send images 1, 2, 3 via email" → {"type": "email_images", "image_indices": [0, 1, 2]}
- "email these images with subject hello" → {"type": "email_images", "image_indices": [0, 1], "subject": "hello"}

Note: Use 0-based indices (chart 1 = index 0, image 1 = index 0, etc.)"""

    def _format_component_context(self, components: List[Dict[str, Any]]) -> str:
        """Format available components (charts + images) for LLM context"""
        if not components:
            return "No components available."

        component_list = []
        chart_count = 0
        image_count = 0

        for i, component in enumerate(components):
            comp_type = component.get("component_type", "unknown")
            title = component.get("nav_title", "Untitled Component")
            comp_id = component.get("id", "")

            if comp_type == "chart":
                chart_idx = component.get("index", chart_count)
                chart_count += 1
                component_list.append(
                    f"Chart {chart_idx+1} (global index {i}): {title} [{component.get('nav_type', 'unknown')}] (ID: {comp_id})"
                )
            elif comp_type == "image":
                image_idx = component.get("index", image_count)
                image_count += 1
                operation = component.get("nav_operation", "generated")
                component_list.append(
                    f"Image {image_idx+1} (global index {i}): {title} [{operation}] (ID: {comp_id})"
                )
            else:
                component_list.append(
                    f"Component {i+1} (global index {i}): {title} [{comp_type}] (ID: {comp_id})"
                )

        summary = f"Available components ({len(components)} total - {chart_count} charts, {image_count} images):\n"
        return summary + "\n".join(component_list)

    def _format_chart_context(self, charts: List[Dict[str, Any]]) -> str:
        """Format available charts for LLM context - legacy method for backward compatibility"""
        return self._format_component_context(charts)

    def _create_navigation_prompt(self, text: str, component_context: str) -> str:
        """Create the navigation prompt for LLM"""
        return f"""User voice command: "{text}"

{component_context}

Is this a component navigation command? Don't be a strict judge as TTS can make mistakes. Try to map the user query to some navigation command for charts, images, or components. Only if it doesn't make any sense then say not_navigation. If yes, provide the appropriate JSON response. If no, return {{"type": "not_navigation"}}."""

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
        Simplified method that gets available components automatically.

        Args:
            text: User voice input
            session_id: Current session ID

        Returns:
            Dict with navigation action or None if not navigation
        """
        # Get available components (charts + images)
        available_components = []
        if session_id:
            storage = get_session_storage()
            available_components = storage.get_component_registry(session_id)

        return await self.process_navigation_command(
            text, available_components, session_id
        )


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


# ============================================================================
# IMAGE NAVIGATION FUNCTION IMPLEMENTATIONS
# ============================================================================


async def navigate_to_image(params: FunctionCallParams):
    """
    Navigate to a specific image by index or ID.

    Args:
        params: Function call parameters containing image_index, image_id, session_id
    """
    try:
        # Extract parameters
        image_index = params.arguments.get("image_index")
        image_id = params.arguments.get("image_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not session_id:
            await params.result_callback("Error: No session ID available")
            return

        storage = get_session_storage()
        image_metadata = None

        # Try to find image by index or ID
        if image_index is not None:
            image_metadata = storage.get_image_by_index(session_id, image_index)
            if not image_metadata:
                image_count = storage.get_image_count(session_id)
                await params.result_callback(
                    f"Image {image_index + 1} not found. Available images: 1-{image_count}"
                )
                return
        elif image_id:
            image_metadata = storage.get_image_by_id(session_id, image_id)
            if not image_metadata:
                await params.result_callback(f"Image with ID '{image_id}' not found")
                return
        else:
            await params.result_callback(
                "Error: Either image_index or image_id must be provided"
            )
            return

        # Success response
        image_title = image_metadata.get("nav_title", "Generated Image")
        image_number = image_metadata.get("index", 0) + 1

        await params.result_callback(
            f"Navigating to image {image_number}: {image_title}"
        )
        logger.info(
            f"Navigation to image {image_number} requested for session {session_id}"
        )

    except Exception as e:
        error_message = f"Error navigating to image: {str(e)}"
        logger.error(f"navigate_to_image: {error_message}")
        await params.result_callback(error_message)


async def list_images(params: FunctionCallParams):
    """
    List all available images with their basic information.

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
        images = storage.get_image_registry(session_id)

        if not images:
            await params.result_callback("No images available in this session")
            return

        # Format image list
        image_list = []
        for image in images:
            image_number = image.get("index", 0) + 1
            title = image.get("nav_title", "Generated Image")
            operation = image.get("nav_operation", "generated")
            image_list.append(f"Image {image_number}: {title} ({operation})")

        response = f"Available images ({len(images)} total):\n" + "\n".join(image_list)
        await params.result_callback(response)
        logger.info(f"Listed {len(images)} images for session {session_id}")

    except Exception as e:
        error_message = f"Error listing images: {str(e)}"
        logger.error(f"list_images: {error_message}")
        await params.result_callback(error_message)


async def get_image_info(params: FunctionCallParams):
    """
    Get detailed information about a specific image.

    Args:
        params: Function call parameters containing image_index, image_id, session_id
    """
    try:
        # Extract parameters
        image_index = params.arguments.get("image_index")
        image_id = params.arguments.get("image_id")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not session_id:
            await params.result_callback("Error: No session ID available")
            return

        storage = get_session_storage()
        image_metadata = None

        # Try to find image by index or ID
        if image_index is not None:
            image_metadata = storage.get_image_by_index(session_id, image_index)
        elif image_id:
            image_metadata = storage.get_image_by_id(session_id, image_id)
        else:
            await params.result_callback(
                "Error: Either image_index or image_id must be provided"
            )
            return

        if not image_metadata:
            await params.result_callback("Image not found")
            return

        # Format image information
        image_number = image_metadata.get("index", 0) + 1
        title = image_metadata.get("nav_title", "Generated Image")
        description = image_metadata.get("nav_description", "")
        operation = image_metadata.get("nav_operation", "generated")
        created_at = image_metadata.get("created_at", "")

        info_lines = [f"Image {image_number}: {title}", f"Operation: {operation}"]

        if description:
            info_lines.append(f"Description: {description}")

        if created_at:
            info_lines.append(f"Created: {created_at}")

        response = "\n".join(info_lines)
        await params.result_callback(response)
        logger.info(f"Provided info for image {image_number} in session {session_id}")

    except Exception as e:
        error_message = f"Error getting image info: {str(e)}"
        logger.error(f"get_image_info: {error_message}")
        await params.result_callback(error_message)


async def display_multiple_images(params: FunctionCallParams):
    """
    Display multiple images simultaneously.

    Args:
        params: Function call parameters containing image_indices, session_id
    """
    try:
        # Extract parameters
        image_indices = params.arguments.get("image_indices", [])
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not image_indices:
            await params.result_callback("Error: No image indices provided")
            return

        if not session_id:
            await params.result_callback("Error: No session ID available")
            return

        storage = get_session_storage()

        # Validate image indices exist
        image_count = storage.get_image_count(session_id)
        invalid_indices = [i for i in image_indices if i < 0 or i >= image_count]

        if invalid_indices:
            await params.result_callback(
                f"Error: Image indices {invalid_indices} are invalid. Available images: 0-{image_count-1}"
            )
            return

        # Get images for specified indices
        images = storage.get_images_by_indices(session_id, image_indices)

        if not images:
            await params.result_callback("Error: Could not retrieve images for display")
            return

        # Get image titles for response
        image_titles = []
        for image_data in images:
            title = image_data.get("nav_title", "Generated Image")
            image_titles.append(title)

        # Success response
        response_message = f"Displaying images: {', '.join(image_titles)}"

        await params.result_callback(response_message)
        logger.info(
            f"Successfully displayed {len(images)} images for session {session_id}"
        )

    except Exception as e:
        error_message = f"Error displaying multiple images: {str(e)}"
        logger.error(f"display_multiple_images: {error_message}")
        await params.result_callback(error_message)


# ============================================================================
# MIXED COMPONENT FUNCTION IMPLEMENTATIONS
# ============================================================================


async def display_mixed_components(params: FunctionCallParams):
    """
    Display charts and images together in a unified view.

    Args:
        params: Function call parameters containing chart_indices, image_indices, session_id
    """
    try:
        # Extract parameters
        chart_indices = params.arguments.get("chart_indices", [])
        image_indices = params.arguments.get("image_indices", [])
        session_id = params.arguments.get("session_id") or get_current_session_id()

        if not chart_indices and not image_indices:
            await params.result_callback("Error: No chart or image indices provided")
            return

        if not session_id:
            await params.result_callback("Error: No session ID available")
            return

        storage = get_session_storage()
        component_titles = []

        # Validate and collect charts
        if chart_indices:
            chart_count = storage.get_chart_count(session_id)
            invalid_chart_indices = [
                i for i in chart_indices if i < 0 or i >= chart_count
            ]

            if invalid_chart_indices:
                await params.result_callback(
                    f"Error: Chart indices {invalid_chart_indices} are invalid. Available charts: 0-{chart_count-1}"
                )
                return

            charts = storage.get_charts_by_indices(session_id, chart_indices)
            for chart in charts:
                title = chart.get("nav_title", "Untitled Chart")
                component_titles.append(f"Chart: {title}")

        # Validate and collect images
        if image_indices:
            image_count = storage.get_image_count(session_id)
            invalid_image_indices = [
                i for i in image_indices if i < 0 or i >= image_count
            ]

            if invalid_image_indices:
                await params.result_callback(
                    f"Error: Image indices {invalid_image_indices} are invalid. Available images: 0-{image_count-1}"
                )
                return

            images = storage.get_images_by_indices(session_id, image_indices)
            for image in images:
                title = image.get("nav_title", "Generated Image")
                component_titles.append(f"Image: {title}")

        # Success response
        response_message = f"Displaying mixed components: {', '.join(component_titles)}"

        await params.result_callback(response_message)
        logger.info(f"Successfully displayed mixed components for session {session_id}")

    except Exception as e:
        error_message = f"Error displaying mixed components: {str(e)}"
        logger.error(f"display_mixed_components: {error_message}")
        await params.result_callback(error_message)


async def list_components(params: FunctionCallParams):
    """
    List all available components (charts and images) with their information.

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
        components = storage.get_component_registry(session_id)

        if not components:
            await params.result_callback("No components available in this session")
            return

        # Format component list
        component_list = []
        for component in components:
            global_index = component.get("global_index", 0)
            comp_type = component.get("component_type", "unknown")
            title = component.get("nav_title", "Untitled Component")

            if comp_type == "chart":
                chart_idx = component.get("index", 0) + 1
                component_list.append(
                    f"Chart {chart_idx} (global #{global_index}): {title}"
                )
            elif comp_type == "image":
                image_idx = component.get("index", 0) + 1
                component_list.append(
                    f"Image {image_idx} (global #{global_index}): {title}"
                )
            else:
                component_list.append(
                    f"Component {global_index}: {title} ({comp_type})"
                )

        response = f"Available components ({len(components)} total):\n" + "\n".join(
            component_list
        )
        await params.result_callback(response)
        logger.info(f"Listed {len(components)} components for session {session_id}")

    except Exception as e:
        error_message = f"Error listing components: {str(e)}"
        logger.error(f"list_components: {error_message}")
        await params.result_callback(error_message)


async def search_components(params: FunctionCallParams):
    """
    Search for components (charts and images) by title, type, or content.

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
        matching_components = storage.search_components(session_id, search_query)

        if not matching_components:
            await params.result_callback(
                f"No components found matching '{search_query}'"
            )
            return

        # Format results
        results = []
        for component in matching_components:
            global_index = component.get("global_index", 0)
            comp_type = component.get("component_type", "unknown")
            title = component.get("nav_title", "Untitled Component")

            if comp_type == "chart":
                chart_idx = component.get("index", 0) + 1
                results.append(f"Chart {chart_idx} (global #{global_index}): {title}")
            elif comp_type == "image":
                image_idx = component.get("index", 0) + 1
                results.append(f"Image {image_idx} (global #{global_index}): {title}")
            else:
                results.append(f"Component {global_index}: {title} ({comp_type})")

        response = (
            f"Found {len(matching_components)} component(s) matching '{search_query}':\n"
            + "\n".join(results)
        )
        await params.result_callback(response)
        logger.info(
            f"Component search for '{search_query}' returned {len(matching_components)} results"
        )

    except Exception as e:
        error_message = f"Error searching components: {str(e)}"
        logger.error(f"search_components: {error_message}")
        await params.result_callback(error_message)


# Wrapper for tool registration (maintains backward compatibility)
async def summarize_charts_tool(params: FunctionCallParams):
    """Tool-compatible wrapper for summarize_charts function"""
    return await summarize_charts(params, llm_service=None)


# Import email function for tool registration
from app.agents.voice.automatic.tools.email.tools import (
    email_images as email_images_tool,
)

# Export tool functions for LLM integration (now includes image, mixed component, and email functions)
tool_functions = {
    # Chart-specific functions
    "summarize_charts": summarize_charts_tool,
    "navigate_to_chart": navigate_to_chart,
    "search_charts": search_charts,
    "list_charts": list_charts,
    "get_chart_info": get_chart_info,
    # Image-specific functions
    "navigate_to_image": navigate_to_image,
    "list_images": list_images,
    "get_image_info": get_image_info,
    "display_multiple_images": display_multiple_images,
    # Mixed component functions
    "display_mixed_components": display_mixed_components,
    "list_components": list_components,
    "search_components": search_components,
    # Email functions
    "email_images": email_images_tool,
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
                    elif result_type == "display_multiple_images":
                        # For multi-image display, emit multiple image components
                        await self._handle_multi_image_display(result, session_id)
                        return True
                    elif result_type == "display_mixed_components":
                        # For mixed display, emit charts and images together
                        await self._handle_mixed_component_display(result, session_id)
                        return True
                    elif result_type == "display_multiple_components":
                        # For global multi-component display
                        await self._handle_multi_component_display(result, session_id)
                        return True
                    elif result_type == "email_images":
                        # For emailing images
                        await self._handle_email_images(result, session_id)
                        return True
                    elif result_type == "not_navigation":
                        return False
                    else:
                        # Convert LLM result to simple navigation
                        navigation_data = self._convert_llm_result_to_navigation(result)
                        if navigation_data:
                            await self._send_navigation(navigation_data)
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
        """Convert LLM navigation result to simple navigation data"""
        try:
            result_type = llm_result.get("type")

            if result_type == "navigate_to_chart":
                return {
                    "action": "navigate_to_chart",
                    "chart_index": llm_result.get("target_chart_index"),
                    "chart_id": llm_result.get("target_chart_id"),
                    "lastChart": llm_result.get("lastChart", False),
                }
            elif result_type == "navigate_to_image":
                return {
                    "action": "navigate_to_image",
                    "image_index": llm_result.get("target_image_index"),
                    "image_id": llm_result.get("target_image_id"),
                    "lastImage": llm_result.get("lastImage", False),
                }
            elif result_type == "navigate_to_component":
                return {
                    "action": "navigate_to_component",
                    "global_index": llm_result.get("target_global_index"),
                    "component_id": llm_result.get("target_component_id"),
                    "lastComponent": False,
                }
            elif result_type == "navigate_direction":
                return {
                    "action": "navigate",
                    "direction": llm_result.get("direction"),
                    "component_type": llm_result.get("component_type", "any"),
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
            elif result_type == "search_components":
                # For component search, navigate to first result if available
                if llm_result.get("matching_components"):
                    first_match = llm_result["matching_components"][0]
                    comp_type = first_match.get("component_type", "unknown")
                    if comp_type == "chart":
                        return {
                            "action": "navigate_to_chart",
                            "chart_index": first_match.get("index"),
                            "chart_id": first_match.get("id"),
                            "lastChart": False,
                        }
                    elif comp_type == "image":
                        return {
                            "action": "navigate_to_image",
                            "image_index": first_match.get("index"),
                            "image_id": first_match.get("id"),
                            "lastImage": False,
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

    async def _handle_multi_image_display(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle multi-image display by emitting multiple image UI components.

        Args:
            llm_result: The LLM navigation result containing image indices
            session_id: Current session ID
        """
        try:
            # Get image indices from LLM result
            image_indices = llm_result.get("image_indices", [])

            if not image_indices:
                logger.warning(
                    "ChartNavigator: No image indices provided for multi-image display"
                )
                return

            # Get the requested images from session storage
            storage = get_session_storage()
            images = storage.get_images_by_indices(session_id, image_indices)

            if not images:
                logger.warning(
                    f"ChartNavigator: No images found for indices {image_indices}"
                )
                return

            # Convert image metadata back to UI components and emit each one
            ui_components = []
            for image_metadata in images:
                # Reconstruct complete UI component from stored image data
                image_props = image_metadata.get("props", {})

                # Base UI component structure for images
                ui_component = {
                    "id": image_metadata.get("id"),
                    "type": "image",
                    "props": {
                        "imageId": image_metadata.get("id"),
                        "imageUrl": image_metadata.get("url", ""),
                        "title": image_metadata.get("nav_title", "Generated Image"),
                        "description": image_metadata.get("nav_description", ""),
                        "operation": image_metadata.get("nav_operation", "generated"),
                        # Copy ALL original props to preserve image-specific data
                        **image_props,
                    },
                    "metadata": {
                        "index": image_metadata.get("index"),
                        "generatedAt": image_metadata.get("created_at"),
                        "chartType": "image",  # Keep unified field name
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
                                "displayMode": "multi-image",
                                "requestedIndices": image_indices,
                                "totalComponents": len(ui_components),
                                "components": ui_components,
                            },
                        }
                    )
                )

                image_titles = [
                    comp.get("props", {}).get("title", "Unknown")
                    for comp in ui_components
                ]
                logger.info(
                    f"ChartNavigator: Emitted multi-image display with {len(ui_components)} images: {', '.join(image_titles)}"
                )
            else:
                logger.warning(
                    "ChartNavigator: No RTVI processor available for multi-image display"
                )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling multi-image display: {e}")

    async def _handle_mixed_component_display(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle mixed component display (charts + images) by emitting unified UI components.

        Args:
            llm_result: The LLM navigation result containing chart and image indices
            session_id: Current session ID
        """
        try:
            # Get indices from LLM result
            chart_indices = llm_result.get("chart_indices", [])
            image_indices = llm_result.get("image_indices", [])

            if not chart_indices and not image_indices:
                logger.warning(
                    "ChartNavigator: No chart or image indices provided for mixed display"
                )
                return

            storage = get_session_storage()
            ui_components = []

            # Get charts and convert to UI components
            if chart_indices:
                charts = storage.get_charts_by_indices(session_id, chart_indices)
                for chart_metadata in charts:
                    chart_props = chart_metadata.get("props", {})
                    ui_component = {
                        "id": chart_metadata.get("id"),
                        "type": chart_metadata.get("type"),
                        "props": {
                            "chartId": chart_metadata.get("id"),
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

            # Get images and convert to UI components
            if image_indices:
                images = storage.get_images_by_indices(session_id, image_indices)
                for image_metadata in images:
                    image_props = image_metadata.get("props", {})
                    ui_component = {
                        "id": image_metadata.get("id"),
                        "type": "image",
                        "props": {
                            "imageId": image_metadata.get("id"),
                            "imageUrl": image_metadata.get("url", ""),
                            "title": image_metadata.get("nav_title", "Generated Image"),
                            **image_props,
                        },
                        "metadata": {
                            "index": image_metadata.get("index"),
                            "generatedAt": image_metadata.get("created_at"),
                            "chartType": "image",
                        },
                        "uiComponent": True,
                    }
                    ui_components.append(ui_component)

            if not ui_components:
                logger.warning(f"ChartNavigator: No components found for mixed display")
                return

            # Emit all UI components as a single array via RTVI
            if self._rtvi_processor:
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "ui-components",
                            "payload": {
                                "displayMode": "mixed-dashboard",
                                "chartIndices": chart_indices,
                                "imageIndices": image_indices,
                                "totalComponents": len(ui_components),
                                "components": ui_components,
                            },
                        }
                    )
                )

                logger.info(
                    f"ChartNavigator: Emitted mixed component display with {len(ui_components)} components ({len(chart_indices)} charts, {len(image_indices)} images)"
                )
            else:
                logger.warning(
                    "ChartNavigator: No RTVI processor available for mixed component display"
                )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling mixed component display: {e}")

    async def _handle_multi_component_display(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle multi-component display by global indices.

        Args:
            llm_result: The LLM navigation result containing global indices
            session_id: Current session ID
        """
        try:
            # Get global indices from LLM result
            global_indices = llm_result.get("global_indices", [])

            if not global_indices:
                logger.warning(
                    "ChartNavigator: No global indices provided for multi-component display"
                )
                return

            # Get the requested components from session storage
            storage = get_session_storage()
            components = storage.get_components_by_global_indices(
                session_id, global_indices
            )

            if not components:
                logger.warning(
                    f"ChartNavigator: No components found for global indices {global_indices}"
                )
                return

            # Convert component metadata to UI components
            ui_components = []
            for component_metadata in components:
                comp_type = component_metadata.get("component_type", "unknown")

                if comp_type == "chart":
                    chart_props = component_metadata.get("props", {})
                    ui_component = {
                        "id": component_metadata.get("id"),
                        "type": component_metadata.get("type"),
                        "props": {
                            "chartId": component_metadata.get("id"),
                            **chart_props,
                        },
                        "metadata": {
                            "index": component_metadata.get("index"),
                            "globalIndex": component_metadata.get("global_index"),
                            "generatedAt": component_metadata.get("created_at"),
                            "chartType": "chart",
                        },
                        "uiComponent": True,
                    }
                elif comp_type == "image":
                    image_props = component_metadata.get("props", {})
                    ui_component = {
                        "id": component_metadata.get("id"),
                        "type": "image",
                        "props": {
                            "imageId": component_metadata.get("id"),
                            "imageUrl": component_metadata.get("url", ""),
                            "title": component_metadata.get(
                                "nav_title", "Generated Image"
                            ),
                            **image_props,
                        },
                        "metadata": {
                            "index": component_metadata.get("index"),
                            "globalIndex": component_metadata.get("global_index"),
                            "generatedAt": component_metadata.get("created_at"),
                            "chartType": "image",
                        },
                        "uiComponent": True,
                    }
                else:
                    # Generic component handling
                    ui_component = {
                        "id": component_metadata.get("id"),
                        "type": component_metadata.get("type", "unknown"),
                        "props": component_metadata.get("props", {}),
                        "metadata": {
                            "index": component_metadata.get("index"),
                            "globalIndex": component_metadata.get("global_index"),
                            "generatedAt": component_metadata.get("created_at"),
                            "chartType": comp_type,
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
                                "displayMode": "multi-component",
                                "requestedGlobalIndices": global_indices,
                                "totalComponents": len(ui_components),
                                "components": ui_components,
                            },
                        }
                    )
                )

                logger.info(
                    f"ChartNavigator: Emitted multi-component display with {len(ui_components)} components (global indices: {global_indices})"
                )
            else:
                logger.warning(
                    "ChartNavigator: No RTVI processor available for multi-component display"
                )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling multi-component display: {e}")

    async def _handle_email_images(
        self, llm_result: Dict[str, Any], session_id: str
    ) -> None:
        """
        Handle emailing images by calling the email tool function.

        Args:
            llm_result: The LLM navigation result containing email parameters
            session_id: Current session ID
        """
        try:
            # Get image indices from LLM result
            image_indices = llm_result.get("image_indices", [])
            subject = llm_result.get("subject")
            message = llm_result.get("message")

            if not image_indices:
                logger.warning("ChartNavigator: No image indices provided for email")
                return

            logger.info(
                f"ChartNavigator: Handling email request for images {image_indices}"
            )

            # Import email tool function
            from app.agents.voice.automatic.tools.email.tools import email_images

            # Create a mock FunctionCallParams object
            class MockParams:
                def __init__(self, arguments):
                    self.arguments = arguments
                    self._result = None

                async def result_callback(self, result):
                    self._result = result
                    logger.info(f"ChartNavigator: Email result: {result}")

            params = MockParams(
                {
                    "image_indices": image_indices,
                    "subject": subject,
                    "message": message,
                    "session_id": session_id,
                }
            )

            # Call the email function
            await email_images(params)

            logger.info(
                f"ChartNavigator: Email request processed for session {session_id}"
            )

        except Exception as e:
            logger.error(f"ChartNavigator: Error handling email images: {e}")

    async def _send_navigation(self, navigation_data: Dict[str, Any]) -> None:
        """
        Send component navigation to frontend via RTVI.

        Args:
            navigation_data: Navigation data (which component to show)
        """
        try:
            action = navigation_data.get("action", "unknown")

            # Determine the navigation type for RTVI event
            if "chart" in action:
                event_type = "chart-navigation"
            elif "image" in action:
                event_type = "image-navigation"
            elif "component" in action:
                event_type = "component-navigation"
            else:
                event_type = "navigation"

            # Send via RTVI if processor is available
            if self._rtvi_processor:
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": event_type, "payload": navigation_data}
                    )
                )
                logger.info(f"ChartNavigator: Sent {event_type} via RTVI: {action}")
            else:
                # Fallback: log for debugging
                logger.info(
                    f"ChartNavigator: {event_type} (no RTVI): {json.dumps(navigation_data, indent=2)}"
                )
        except Exception as e:
            logger.error(f"ChartNavigator: Error sending navigation: {e}")

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
