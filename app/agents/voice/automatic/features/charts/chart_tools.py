"""
Chart generation tools for LLM function calling.
The LLM can call these tools to generate interactive charts with voice narration.
"""

from datetime import datetime
from typing import Any, Dict, List

from app.agents.voice.automatic.features.charts.session_storage import (
    get_session_storage,
)
from app.agents.voice.automatic.features.charts.types.ui_components import (
    UIComponentEvent,
)
from app.agents.voice.automatic.features.charts.utils.highlight_parser import (
    HighlightTagParser,
)
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.core import config
from app.core.logger import logger

# Color constants matching MCP implementation
DEFAULT_COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b"]
DONUT_COLORS = ["#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#e377c2"]


class ChartTurnManager:
    """Manages chart emission tracking per session to enforce turn limits."""

    def __init__(self):
        self._pending_chart_emissions: Dict[str, List[Dict[str, Any]]] = {}
        self._chart_turn_counts: Dict[str, int] = {}
        self._session_has_hitl: Dict[str, bool] = {}

    def reset_turn_state(self, session_id: str) -> None:
        """Reset chart count and HITL flag for a new LLM turn."""
        self._chart_turn_counts[session_id] = 0
        self._session_has_hitl[session_id] = False

    def mark_hitl_operation(self, session_id: str) -> None:
        """Mark that a HITL operation occurred in this turn."""
        self._session_has_hitl[session_id] = True

    def register_pending_chart_emission(
        self, session_id: str, component_data: Dict[str, Any]
    ) -> None:
        """
        Register a chart component for RTVI emission.
        """
        if session_id not in self._chart_turn_counts:
            self._chart_turn_counts[session_id] = 0

        # Check if any tool result in this turn was a HITL operation
        if self._session_has_hitl.get(session_id, False):
            logger.warning(
                f"[{session_id}] Silently rejecting chart '{component_data.get('props', {}).get('title', 'Unknown')}' - "
                f"HITL operation detected in current turn"
            )
            return

        if self._chart_turn_counts[session_id] >= config.MAX_CHARTS_PER_TURN:
            logger.warning(
                f"[{session_id}] Silently rejecting chart '{component_data.get('props', {}).get('title', 'Unknown')}' - "
                f"maximum {config.MAX_CHARTS_PER_TURN} charts per turn exceeded (count: {self._chart_turn_counts[session_id] + 1})"
            )
            return

        self._chart_turn_counts[session_id] += 1

        if session_id not in self._pending_chart_emissions:
            self._pending_chart_emissions[session_id] = []
        self._pending_chart_emissions[session_id].append(component_data)

    def get_pending_chart_emissions(self, session_id: str) -> List[Dict[str, Any]]:
        """Get and clear pending chart emissions for a session."""
        charts = self._pending_chart_emissions.get(session_id, [])
        if session_id in self._pending_chart_emissions:
            del self._pending_chart_emissions[session_id]
        return charts


_default_chart_turn_manager = ChartTurnManager()


def generate_chart_id(chart_type: str) -> str:
    """Generate chart ID matching MCP format: {chartType}_{timestamp}"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{chart_type}_{timestamp}"


def get_pending_ui_components(session_id: str) -> List[UIComponentEvent]:
    """
    Retrieve and clear pending UI components for a session.
    Called by WebSocket handler to get components to emit.
    """
    try:
        storage = get_session_storage()
        return storage.get_pending_ui_components(session_id)
    except Exception as e:
        logger.error(
            f"Tool Error: [get_pending_ui_components] [{session_id}] Failed to retrieve UI components: {e}"
        )
        return []


def reset_chart_turn_state(session_id: str) -> None:
    """Reset chart count and actionable flag for a new LLM turn."""
    _default_chart_turn_manager.reset_turn_state(session_id)


def mark_hitl_operation(session_id: str) -> None:
    """Mark that a HITL operation occurred in this turn."""
    _default_chart_turn_manager.mark_hitl_operation(session_id)


def _register_pending_chart_emission(
    session_id: str, component_data: Dict[str, Any]
) -> None:
    """Register a chart component for RTVI emission."""
    _default_chart_turn_manager.register_pending_chart_emission(
        session_id, component_data
    )


def get_pending_chart_emissions(session_id: str) -> List[Dict[str, Any]]:
    """Get and clear pending chart emissions for a session."""
    return _default_chart_turn_manager.get_pending_chart_emissions(session_id)


async def generate_bar_chart(params) -> None:
    """Generate a bar chart component for comparative data analysis."""
    try:
        # Extract parameters from LLM call
        title = params.arguments.get("title")
        categories = params.arguments.get("categories")
        series_data = params.arguments.get("series_data")
        voice_description = params.arguments.get("voice_description")
        subtitle = params.arguments.get("subtitle")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        # Validate series_data - only one series allowed like MCP
        if len(series_data) > 1:
            await params.result_callback(
                "Tool Error: [generate_bar_chart] Only one series allowed for bar charts"
            )
            return

        # Generate chart ID using MCP format
        chart_id = generate_chart_id("bar_chart")

        # Process voice description with MCP-style highlight parsing
        highlight_parser = HighlightTagParser()
        processed = highlight_parser.parse_highlight_tags(
            voice_description,
            {
                "categories": categories,
                "chartType": "bar-chart",
                "chartId": chart_id,
                "sessionId": session_id,
            },
        )

        # Create UI component matching MCP structure
        ui_component = {
            "id": chart_id,
            "type": "bar-chart",
            "props": {
                "chartId": chart_id,
                "title": title,
                "subtitle": subtitle,
                "categories": categories,
                "series": [
                    {
                        "name": s.get("name", "Data"),
                        "data": s.get("data", []),
                        "color": s.get("color")
                        or DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                    }
                    for i, s in enumerate(series_data)
                ],
            },
            "voiceDescription": processed["originalText"],
            "renderOrder": 0,
            "metadata": {
                "generatedAt": datetime.now().isoformat(),
                "chartType": "chart",
                "confidence": 0.95,
                "status": "completed",
                "cleanVoiceDescription": processed["cleanText"],
                "highlights": processed["highlights"],
            },
            "uiComponent": True,
        }

        # Store for RTVI emission
        _register_pending_chart_emission(session_id, ui_component)

        logger.info(
            f"[{session_id}] Generated bar chart: {title} with {len(categories)} categories"
        )

        # Return clean voice description to LLM
        await params.result_callback(processed["cleanText"])

    except Exception as e:
        error_message = str(e)
        logger.error(
            f"Tool Error: [generate_bar_chart] [{session_id}] Error generating bar chart: {error_message}"
        )
        await params.result_callback(
            f"Tool Error: [generate_bar_chart] Error generating bar chart: {error_message}"
        )


async def generate_line_chart(params) -> None:
    """Generate a line chart component for time series or trend analysis."""
    try:
        # Extract parameters from LLM call
        title = params.arguments.get("title")
        categories = params.arguments.get("categories")
        series_data = params.arguments.get("series_data")
        voice_description = params.arguments.get("voice_description")
        subtitle = params.arguments.get("subtitle")
        data_type = params.arguments.get("data_type", "unknown")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        # Generate chart ID using MCP format
        chart_id = generate_chart_id("line_chart")

        # Process voice description with MCP-style highlight parsing
        highlight_parser = HighlightTagParser()
        processed = highlight_parser.parse_highlight_tags(
            voice_description,
            {
                "categories": categories,
                "chartType": "line-chart",
                "chartId": chart_id,
                "sessionId": session_id,
            },
        )

        # Create UI component matching MCP structure
        ui_component = {
            "id": chart_id,
            "type": "line-chart",
            "props": {
                "chartId": chart_id,
                "title": title,
                "subtitle": subtitle,
                "categories": categories,
                "dataType": data_type,
                "series": [
                    {
                        "name": s.get("name", "Trend"),
                        "data": s.get("data", []),
                        "dataLabel": categories,  # MCP adds this
                        "dashStyle": "Solid",  # MCP adds this
                        "color": s.get("color")
                        or DEFAULT_COLORS[i % len(DEFAULT_COLORS)],
                    }
                    for i, s in enumerate(series_data)
                ],
            },
            "voiceDescription": processed["originalText"],
            "renderOrder": 0,
            "metadata": {
                "generatedAt": datetime.now().isoformat(),
                "chartType": "chart",
                "confidence": 0.95,
                "status": "completed",
                "cleanVoiceDescription": processed["cleanText"],
                "highlights": processed["highlights"],
            },
            "uiComponent": True,
        }

        # Store for RTVI emission
        _register_pending_chart_emission(session_id, ui_component)

        logger.info(
            f"[{session_id}] Generated line chart: {title} with {len(categories)} time points"
        )

        # Return clean voice description to LLM
        await params.result_callback(processed["cleanText"])

    except Exception as e:
        error_message = str(e)
        logger.error(
            f"Tool Error: [generate_line_chart] [{session_id}] Error generating line chart: {error_message}"
        )
        await params.result_callback(
            f"Tool Error: [generate_line_chart] Error generating line chart: {error_message}"
        )


async def generate_donut_chart(params) -> None:
    """Generate a donut chart component for percentage/proportion analysis."""
    try:
        # Extract parameters from LLM call
        title = params.arguments.get("title")
        categories = params.arguments.get("categories")
        data = params.arguments.get("data")
        data_type = params.arguments.get("data_type")
        voice_description = params.arguments.get("voice_description")
        subtitle = params.arguments.get("subtitle")
        colors = params.arguments.get("colors")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        # Validate data_type parameter
        valid_data_types = ["currency", "numericalValue", "percentage", "unknown"]
        if data_type not in valid_data_types:
            await params.result_callback(
                f"Tool Error: [generate_donut_chart] Invalid data_type. Must be one of: {valid_data_types}"
            )
            return

        # Generate chart ID using MCP format
        chart_id = generate_chart_id("donut_chart")

        # Process voice description with MCP-style highlight parsing
        highlight_parser = HighlightTagParser()
        processed = highlight_parser.parse_highlight_tags(
            voice_description,
            {
                "categories": categories,
                "chartType": "donut-chart",
                "chartId": chart_id,
                "sessionId": session_id,
            },
        )

        # Use MCP donut colors if not provided
        if colors is None or len(colors) != len(categories):
            colors = DONUT_COLORS[: len(categories)]

        # Calculate and format total based on data type (matching MCP logic)
        total_value = sum(data)
        formatted_total = None

        if data_type == "currency":
            # Format as Indian currency with Indian numbering system
            if total_value < 1000:
                formatted_total = f"₹{total_value:,.0f}"
            elif total_value < 100000:  # Less than 1 lakh
                formatted_total = f"₹{total_value/1000:.1f}K"
            elif total_value < 10000000:  # Less than 1 crore
                formatted_total = f"₹{total_value/100000:.1f}L"
            else:  # 1 crore or more
                formatted_total = f"₹{total_value/10000000:.1f}Cr"
        elif data_type == "numericalValue":
            # Indian numbering system for numerical values
            if total_value < 1000:
                formatted_total = f"{total_value:,.0f}"
            elif total_value < 100000:  # Less than 1 lakh
                formatted_total = f"{total_value/1000:.1f}K"
            elif total_value < 10000000:  # Less than 1 crore
                formatted_total = f"{total_value/100000:.1f}L"
            else:  # 1 crore or more
                formatted_total = f"{total_value/10000000:.1f}Cr"
        # For 'percentage' and 'unknown', formatted_total remains None

        # Create UI component matching MCP structure
        ui_component = {
            "id": chart_id,
            "type": "donut-chart",
            "props": {
                "chartId": chart_id,
                "title": title,
                "subtitle": subtitle,
                "categories": categories,
                "series": [{"name": title, "data": data}],
                "colors": colors,
                "totalValue": formatted_total,
                "dataType": data_type,
            },
            "voiceDescription": processed["originalText"],
            "renderOrder": 0,
            "metadata": {
                "generatedAt": datetime.now().isoformat(),
                "chartType": "chart",
                "confidence": 0.95,
                "status": "completed",
                "cleanVoiceDescription": processed["cleanText"],
                "highlights": processed["highlights"],
            },
            "uiComponent": True,
        }

        # Store for RTVI emission
        _register_pending_chart_emission(session_id, ui_component)

        logger.info(
            f"[{session_id}] Generated donut chart: {title} with {len(categories)} segments"
        )

        # Return clean voice description to LLM
        await params.result_callback(processed["cleanText"])

    except Exception as e:
        error_message = str(e)
        logger.error(
            f"Tool Error: [generate_donut_chart] [{session_id}] Error generating donut chart: {error_message}"
        )
        await params.result_callback(
            f"Tool Error: [generate_donut_chart] Error generating donut chart: {error_message}"
        )


async def generate_single_stat_card(params) -> None:
    """Generate a single statistic card showing a key metric."""
    try:
        # Extract parameters from LLM call
        title = params.arguments.get("title")
        primary_value = params.arguments.get("primary_value")
        metric_name = params.arguments.get("metric_name")
        voice_description = params.arguments.get("voice_description")
        delta_value = params.arguments.get("delta_value")
        delta_positive = params.arguments.get("delta_positive", True)
        date_range = params.arguments.get("date_range")
        data_type = params.arguments.get("data_type", "unknown")
        session_id = params.arguments.get("session_id") or get_current_session_id()

        # Format primary_value based on data_type
        formatted_primary_value = primary_value
        if isinstance(primary_value, (int, float)):
            if data_type == "currency":
                # Format as Indian currency with Indian numbering system
                if primary_value < 1000:
                    formatted_primary_value = f"₹{primary_value:,.0f}"
                elif primary_value < 100000:  # Less than 1 lakh
                    formatted_primary_value = f"₹{primary_value/1000:.1f}K"
                elif primary_value < 10000000:  # Less than 1 crore
                    formatted_primary_value = f"₹{primary_value/100000:.1f}L"
                else:  # 1 crore or more
                    formatted_primary_value = f"₹{primary_value/10000000:.1f}Cr"
            elif data_type == "numericalValue":
                # Indian numbering system for numerical values
                if primary_value < 1000:
                    formatted_primary_value = f"{primary_value:,.0f}"
                elif primary_value < 100000:  # Less than 1 lakh
                    formatted_primary_value = f"{primary_value/1000:.1f}K"
                elif primary_value < 10000000:  # Less than 1 crore
                    formatted_primary_value = f"{primary_value/100000:.1f}L"
                else:  # 1 crore or more
                    formatted_primary_value = f"{primary_value/10000000:.1f}Cr"
            elif data_type == "percentage":
                formatted_primary_value = f"{primary_value}%"
            # For 'unknown', keep original value

        # Generate chart ID using MCP format
        chart_id = generate_chart_id("single_stat_card")

        # Process voice description with MCP-style highlight parsing
        highlight_parser = HighlightTagParser()
        processed = highlight_parser.parse_highlight_tags(
            voice_description,
            {
                "categories": [metric_name],
                "chartType": "single-stat-card",
                "chartId": chart_id,
                "sessionId": session_id,
            },
        )

        # Create UI component matching MCP structure
        ui_component = {
            "id": chart_id,
            "type": "single-stat-card",
            "props": {
                "chartId": chart_id,
                "title": title,
                "chartTitle": title,
                "dateRange": date_range or "",
                "primaryValue": formatted_primary_value,
                "deltaValue": delta_value or "",
                "deltaPositive": delta_positive,
                "metricName": metric_name,
            },
            "voiceDescription": processed["originalText"],
            "renderOrder": 0,
            "metadata": {
                "generatedAt": datetime.now().isoformat(),
                "chartType": "metric",
                "confidence": 0.95,
                "status": "completed",
                "cleanVoiceDescription": processed["cleanText"],
                "highlights": processed["highlights"],
            },
            "uiComponent": True,
        }

        # Store for RTVI emission
        _register_pending_chart_emission(session_id, ui_component)

        logger.info(
            f"[{session_id}] Generated single stat card: {title} - {metric_name}"
        )

        # Return clean voice description to LLM
        await params.result_callback(processed["cleanText"])

    except Exception as e:
        error_message = str(e)
        logger.error(
            f"Tool Error: [generate_single_stat_card] [{session_id}] Error generating start_card : {error_message}"
        )
        await params.result_callback(
            f"Tool Error: [generate_single_stat_card] Error generating start_card : {error_message}"
        )
