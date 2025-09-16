"""
Enhanced session storage for UI components and chart navigation.
Stores pending UI components and maintains chart registry for navigation.
"""

from typing import Dict, List, Any, Optional
from datetime import datetime
from app.agents.voice.automatic.features.charts.types.ui_components import (
    UIComponentEvent,
)
from app.core.logger import logger


class SessionStorage:
    """Simple in-memory storage for session data"""

    def __init__(self):
        self.pending_ui_components: Dict[str, List[UIComponentEvent]] = {}
        self.chart_registry: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> chart metadata
        self.chart_full_data: Dict[str, List[Dict[str, Any]]] = {}  # session_id -> full chart data
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
        if session_id not in self.chart_full_data:
            self.chart_full_data[session_id] = []
        
        # Store full chart data for summarization
        current_index = len(self.chart_registry[session_id])
        full_chart_data = {
            **chart_data,
            "index": current_index,
            "created_at": datetime.now().isoformat()
        }
        self.chart_full_data[session_id].append(full_chart_data)
        
        # Extract metadata for navigation
        chart_metadata = {
            "id": chart_data.get("id"),
            "type": chart_data.get("type"),
            "title": chart_data.get("props", {}).get("title", "Untitled Chart"),
            "subtitle": chart_data.get("props", {}).get("subtitle"),
            "chart_type": chart_data.get("props", {}).get("chartId"),
            "categories": chart_data.get("props", {}).get("categories", []),
            "series_names": [s.get("name", "") for s in chart_data.get("props", {}).get("series", [])],
            "created_at": datetime.now().isoformat(),
            "index": current_index  # 0-based index
        }
        
        self.chart_registry[session_id].append(chart_metadata)
        logger.info(f"SessionStorage: Registered chart '{chart_metadata['title']}' (index: {chart_metadata['index']}) for session {session_id}")
    
    def get_chart_registry(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all charts for a session"""
        return self.chart_registry.get(session_id, [])
    
    def search_charts(self, session_id: str, query: str) -> List[Dict[str, Any]]:
        """Search charts by title, type, or content"""
        charts = self.get_chart_registry(session_id)
        if not charts or not query:
            return []
        
        query_lower = query.lower()
        matching_charts = []
        
        for chart in charts:
            # Search in title
            if query_lower in chart.get("title", "").lower():
                matching_charts.append(chart)
                continue
            
            # Search in subtitle
            if chart.get("subtitle") and query_lower in chart.get("subtitle", "").lower():
                matching_charts.append(chart)
                continue
            
            # Search in chart type
            if query_lower in chart.get("type", "").lower():
                matching_charts.append(chart)
                continue
            
            # Search in categories
            categories = chart.get("categories", [])
            if any(query_lower in cat.lower() for cat in categories if isinstance(cat, str)):
                matching_charts.append(chart)
                continue
            
            # Search in series names
            series_names = chart.get("series_names", [])
            if any(query_lower in name.lower() for name in series_names if isinstance(name, str)):
                matching_charts.append(chart)
                continue
        
        logger.info(f"SessionStorage: Found {len(matching_charts)} charts matching '{query}' for session {session_id}")
        return matching_charts
    
    def get_chart_by_index(self, session_id: str, index: int) -> Optional[Dict[str, Any]]:
        """Get chart by its index (0-based)"""
        charts = self.get_chart_registry(session_id)
        if 0 <= index < len(charts):
            return charts[index]
        return None
    
    def get_chart_by_id(self, session_id: str, chart_id: str) -> Optional[Dict[str, Any]]:
        """Get chart by its ID"""
        charts = self.get_chart_registry(session_id)
        for chart in charts:
            if chart.get("id") == chart_id:
                return chart
        return None
    
    def get_chart_count(self, session_id: str) -> int:
        """Get the number of charts for a session"""
        return len(self.chart_registry.get(session_id, []))
    
    def get_chart_full_data(self, session_id: str, indices: List[int]) -> List[Dict[str, Any]]:
        """Get full chart data for specified indices"""
        if session_id not in self.chart_full_data:
            return []
        
        full_data = self.chart_full_data[session_id]
        result = []
        
        for index in indices:
            if 0 <= index < len(full_data):
                result.append(full_data[index])
            else:
                logger.warning(f"SessionStorage: Chart index {index} not found for session {session_id}")
        
        return result
    
    def get_chart_full_data_by_id(self, session_id: str, chart_id: str) -> Optional[Dict[str, Any]]:
        """Get full chart data by chart ID"""
        if session_id not in self.chart_full_data:
            return None
        
        for chart_data in self.chart_full_data[session_id]:
            if chart_data.get("id") == chart_id:
                return chart_data
        return None
    
    def clear_chart_registry(self, session_id: str):
        """Clear all charts for a session"""
        chart_count = 0
        if session_id in self.chart_registry:
            chart_count = len(self.chart_registry[session_id])
            del self.chart_registry[session_id]
        if session_id in self.chart_full_data:
            del self.chart_full_data[session_id]
        logger.info(f"SessionStorage: Cleared {chart_count} charts for session {session_id}")


# Global session storage instance
_session_storage = SessionStorage()


def get_session_storage() -> SessionStorage:
    """Get the global session storage instance"""
    return _session_storage


def register_chart_for_navigation(session_id: str, chart_data: Dict[str, Any]):
    """Convenience function to register a chart for navigation"""
    storage = get_session_storage()
    storage.register_chart(session_id, chart_data)
