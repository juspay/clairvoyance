"""
Simplified session storage for UI components and basic chart registry.
Stores pending UI components and maintains minimal chart registry for navigation.
"""

from datetime import datetime
from typing import Any, Dict, List

from app.agents.voice.automatic.features.charts.types.ui_components import (
    UIComponentEvent,
)
from app.core.logger import logger


class SessionStorage:
    """Simple in-memory storage for session data"""

    def __init__(self):
        self.pending_ui_components: Dict[str, List[UIComponentEvent]] = {}
        self.chart_registry: Dict[str, List[Dict[str, Any]]] = (
            {}
        )  # session_id -> chart metadata
        self.data: Dict[str, Any] = {}

    def store_ui_component(self, session_id: str, component: UIComponentEvent):
        """Store a UI component for emission"""
        if session_id not in self.pending_ui_components:
            self.pending_ui_components[session_id] = []
        self.pending_ui_components[session_id].append(component)

    def get_pending_ui_components(self, session_id: str) -> List[UIComponentEvent]:
        """Get and clear pending UI components for a session"""
        components = self.pending_ui_components.get(session_id, [])
        if session_id in self.pending_ui_components:
            del self.pending_ui_components[session_id]
        return components

    def set_data(self, key: str, value: Any):
        """Store arbitrary data"""
        self.data[key] = value

    def get_data(self, key: str, default=None):
        """Retrieve arbitrary data"""
        return self.data.get(key, default)

    def register_chart(self, session_id: str, chart_data: Dict[str, Any]):
        """Register a chart in the session's chart registry"""
        if session_id not in self.chart_registry:
            self.chart_registry[session_id] = []

        # Store complete chart data (needed for summarization) + metadata for navigation
        current_index = len(self.chart_registry[session_id])
        chart_metadata = {
            # Full chart data (required for summarization)
            **chart_data,  # Store complete chart data
            # Navigation metadata
            "index": current_index,  # 0-based index
            "created_at": datetime.now().isoformat(),
            # Quick access fields for navigation
            "nav_title": chart_data.get("props", {}).get("title", "Untitled Chart"),
            "nav_type": chart_data.get("type"),
            "nav_categories": chart_data.get("props", {}).get("categories", []),
            "nav_series_names": [
                s.get("name", "") for s in chart_data.get("props", {}).get("series", [])
            ],
        }

        self.chart_registry[session_id].append(chart_metadata)
        logger.info(
            f"SessionStorage: Registered chart '{chart_metadata['nav_title']}' (index: {chart_metadata['index']}) for session {session_id}"
        )

    def get_chart_registry(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all charts for a session"""
        return self.chart_registry.get(session_id, [])

    def get_chart_by_index(self, session_id: str, index: int) -> Dict[str, Any] | None:
        """Get chart by its index (0-based)"""
        charts = self.get_chart_registry(session_id)
        if 0 <= index < len(charts):
            return charts[index]
        return None

    def get_chart_by_id(self, session_id: str, chart_id: str) -> Dict[str, Any] | None:
        """Get chart by its ID"""
        charts = self.get_chart_registry(session_id)
        for chart in charts:
            if chart.get("id") == chart_id:
                return chart
        return None

    def get_chart_count(self, session_id: str) -> int:
        """Get the number of charts for a session"""
        return len(self.chart_registry.get(session_id, []))

    def get_charts_by_indices(
        self, session_id: str, indices: List[int]
    ) -> List[Dict[str, Any]]:
        """Get multiple charts by their indices (0-based)"""
        charts = self.get_chart_registry(session_id)
        result = []

        for index in indices:
            if 0 <= index < len(charts):
                result.append(charts[index])
            else:
                logger.warning(
                    f"SessionStorage: Chart index {index} out of range for session {session_id} (total: {len(charts)})"
                )

        return result

    def clear_chart_registry(self, session_id: str):
        """Clear all charts for a session"""
        chart_count = 0
        if session_id in self.chart_registry:
            chart_count = len(self.chart_registry[session_id])
            del self.chart_registry[session_id]
        logger.info(
            f"SessionStorage: Cleared {chart_count} charts for session {session_id}"
        )


# Global session storage instance
_session_storage = SessionStorage()


def get_session_storage() -> SessionStorage:
    """Get the global session storage instance"""
    return _session_storage


def register_chart_for_navigation(session_id: str, chart_data: Dict[str, Any]):
    """Convenience function to register a chart for navigation"""
    storage = get_session_storage()
    storage.register_chart(session_id, chart_data)
