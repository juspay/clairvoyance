"""
Unified session storage for UI components including charts and images.
Stores pending UI components and maintains registries for navigation.
"""

from datetime import datetime
from typing import Any, Dict, List

from app.agents.voice.automatic.features.charts.types.ui_components import (
    UIComponentEvent,
)
from app.core.logger import logger


class SessionStorage:
    """Unified in-memory storage for session data including charts and images"""

    def __init__(self):
        self.pending_ui_components: Dict[str, List[UIComponentEvent]] = {}
        self.chart_registry: Dict[str, List[Dict[str, Any]]] = (
            {}
        )  # session_id -> chart metadata
        self.image_registry: Dict[str, List[Dict[str, Any]]] = (
            {}
        )  # session_id -> image metadata
        self.component_registry: Dict[str, List[Dict[str, Any]]] = (
            {}
        )  # session_id -> unified components (charts + images)
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
            "component_type": "chart",  # Unified type field
            # Quick access fields for navigation
            "nav_title": chart_data.get("props", {}).get("title", "Untitled Chart"),
            "nav_type": chart_data.get("type"),
            "nav_categories": chart_data.get("props", {}).get("categories", []),
            "nav_series_names": [
                s.get("name", "") for s in chart_data.get("props", {}).get("series", [])
            ],
        }

        self.chart_registry[session_id].append(chart_metadata)
        # Also add to unified component registry
        self._register_unified_component(session_id, chart_metadata)
        logger.info(
            f"SessionStorage: Registered chart '{chart_metadata['nav_title']}' (index: {chart_metadata['index']}) for session {session_id}"
        )

    def register_image(self, session_id: str, image_data: Dict[str, Any]):
        """Register an image in the session's image registry"""
        if session_id not in self.image_registry:
            self.image_registry[session_id] = []

        # Store complete image data + metadata for navigation
        current_index = len(self.image_registry[session_id])
        image_metadata = {
            # Full image data
            **image_data,
            # Navigation metadata
            "index": current_index,  # 0-based index within images
            "created_at": datetime.now().isoformat(),
            "component_type": "image",  # Unified type field
            # Quick access fields for navigation
            "nav_title": image_data.get("props", {}).get(
                "title", image_data.get("title", "Generated Image")
            ),
            "nav_type": "image",
            "nav_description": image_data.get("props", {}).get(
                "description", image_data.get("description", "")
            ),
            "nav_operation": image_data.get("props", {}).get("operation", "generate"),
        }

        self.image_registry[session_id].append(image_metadata)
        # Also add to unified component registry
        self._register_unified_component(session_id, image_metadata)
        logger.info(
            f"SessionStorage: Registered image '{image_metadata['nav_title']}' (index: {image_metadata['index']}) for session {session_id}"
        )

    def _register_unified_component(
        self, session_id: str, component_data: Dict[str, Any]
    ):
        """Register a component in the unified registry (charts + images)"""
        if session_id not in self.component_registry:
            self.component_registry[session_id] = []

        # Calculate global index across all components
        global_index = len(self.component_registry[session_id])
        component_data["global_index"] = global_index
        self.component_registry[session_id].append(component_data)

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

    # Image-specific methods
    def get_image_registry(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all images for a session"""
        return self.image_registry.get(session_id, [])

    def get_image_by_index(self, session_id: str, index: int) -> Dict[str, Any] | None:
        """Get image by its index (0-based within images)"""
        images = self.get_image_registry(session_id)
        if 0 <= index < len(images):
            return images[index]
        return None

    def get_image_by_id(self, session_id: str, image_id: str) -> Dict[str, Any] | None:
        """Get image by its ID"""
        images = self.get_image_registry(session_id)
        for image in images:
            if image.get("id") == image_id:
                return image
        return None

    def get_image_count(self, session_id: str) -> int:
        """Get the number of images for a session"""
        return len(self.image_registry.get(session_id, []))

    def get_images_by_indices(
        self, session_id: str, indices: List[int]
    ) -> List[Dict[str, Any]]:
        """Get multiple images by their indices (0-based)"""
        images = self.get_image_registry(session_id)
        result = []

        for index in indices:
            if 0 <= index < len(images):
                result.append(images[index])
            else:
                logger.warning(
                    f"SessionStorage: Image index {index} out of range for session {session_id} (total: {len(images)})"
                )

        return result

    # Unified component methods
    def get_component_registry(self, session_id: str) -> List[Dict[str, Any]]:
        """Get all components (charts + images) for a session"""
        return self.component_registry.get(session_id, [])

    def get_component_by_global_index(
        self, session_id: str, global_index: int
    ) -> Dict[str, Any] | None:
        """Get component by its global index (0-based across all components)"""
        components = self.get_component_registry(session_id)
        if 0 <= global_index < len(components):
            return components[global_index]
        return None

    def get_components_by_global_indices(
        self, session_id: str, global_indices: List[int]
    ) -> List[Dict[str, Any]]:
        """Get multiple components by their global indices (0-based)"""
        components = self.get_component_registry(session_id)
        result = []

        for index in global_indices:
            if 0 <= index < len(components):
                result.append(components[index])
            else:
                logger.warning(
                    f"SessionStorage: Component index {index} out of range for session {session_id} (total: {len(components)})"
                )

        return result

    def get_component_count(self, session_id: str) -> int:
        """Get the total number of components (charts + images) for a session"""
        return len(self.component_registry.get(session_id, []))

    def search_components(self, session_id: str, query: str) -> List[Dict[str, Any]]:
        """Search for components by title, type, or content"""
        components = self.get_component_registry(session_id)
        matching_components = []

        query_lower = query.lower()
        for component in components:
            # Search in title
            if query_lower in component.get("nav_title", "").lower():
                matching_components.append(component)
                continue
            # Search in description (for images)
            if query_lower in component.get("nav_description", "").lower():
                matching_components.append(component)
                continue
            # Search in categories (for charts)
            categories = component.get("nav_categories", [])
            if any(query_lower in str(cat).lower() for cat in categories):
                matching_components.append(component)
                continue

        return matching_components

    def clear_chart_registry(self, session_id: str):
        """Clear all charts for a session"""
        chart_count = 0
        if session_id in self.chart_registry:
            chart_count = len(self.chart_registry[session_id])
            del self.chart_registry[session_id]
        logger.info(
            f"SessionStorage: Cleared {chart_count} charts for session {session_id}"
        )

    def clear_image_registry(self, session_id: str):
        """Clear all images for a session"""
        image_count = 0
        if session_id in self.image_registry:
            image_count = len(self.image_registry[session_id])
            del self.image_registry[session_id]
        logger.info(
            f"SessionStorage: Cleared {image_count} images for session {session_id}"
        )

    def clear_all_registries(self, session_id: str):
        """Clear all data for a session"""
        chart_count = self.get_chart_count(session_id)
        image_count = self.get_image_count(session_id)

        if session_id in self.chart_registry:
            del self.chart_registry[session_id]
        if session_id in self.image_registry:
            del self.image_registry[session_id]
        if session_id in self.component_registry:
            del self.component_registry[session_id]

        logger.info(
            f"SessionStorage: Cleared {chart_count} charts and {image_count} images for session {session_id}"
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


def register_image_for_navigation(session_id: str, image_data: Dict[str, Any]):
    """Convenience function to register an image for navigation"""
    storage = get_session_storage()
    storage.register_image(session_id, image_data)
