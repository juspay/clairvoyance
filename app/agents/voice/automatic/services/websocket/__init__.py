"""
WebSocket services for the voice agent system.
Provides WebSocket communication for chart navigation and other features.
"""

from .navigation_emitter import NavigationWebSocketEmitter

__all__ = ["NavigationWebSocketEmitter"]