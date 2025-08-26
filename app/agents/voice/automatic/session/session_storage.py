"""
Simple session storage for UI components.
Stores pending UI components that need to be emitted via WebSocket.
"""

from typing import Dict, List, Any
from app.types.ui_components import UIComponentEvent


class SessionStorage:
    """Simple in-memory storage for session data"""
    
    def __init__(self):
        self.pending_ui_components: Dict[str, List[UIComponentEvent]] = {}
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


# Global session storage instance
_session_storage = SessionStorage()


def get_session_storage() -> SessionStorage:
    """Get the global session storage instance"""
    return _session_storage