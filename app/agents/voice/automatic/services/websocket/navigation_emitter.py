"""
WebSocket navigation emitter for chart navigation responses.
Integrates with the existing RTVI WebSocket system to send navigation commands to frontend.
"""

import json
import time
from typing import Dict, Any, Optional
from app.core.logger import logger


class NavigationWebSocketEmitter:
    """
    WebSocket emitter for chart navigation responses.
    
    Integrates with the existing RTVI/Daily transport system to send
    navigation commands and responses to the frontend.
    """
    
    def __init__(self, transport=None):
        """
        Initialize the navigation WebSocket emitter.
        
        Args:
            transport: The WebSocket transport (Daily/RTVI transport)
        """
        self._transport = transport
        logger.info("NavigationWebSocketEmitter initialized")
    
    def set_transport(self, transport) -> None:
        """Set the WebSocket transport for sending messages"""
        self._transport = transport
        logger.info("NavigationWebSocketEmitter: Transport set")
    
    async def emit_navigation_response(self, response: Dict[str, Any]) -> None:
        """
        Emit a navigation response to the frontend via WebSocket.
        
        Args:
            response: Navigation response data
        """
        try:
            if not self._transport:
                logger.warning("NavigationWebSocketEmitter: No transport available, cannot emit response")
                return
            
            # Format the message for RTVI/Daily transport
            navigation_message = {
                "type": "navigation-response",
                "data": response
            }
            
            # Send via the transport's app message system
            if hasattr(self._transport, 'send_app_message'):
                await self._transport.send_app_message(navigation_message)
                logger.info(f"NavigationWebSocketEmitter: Sent navigation response: {response.get('type', 'unknown')}")
            elif hasattr(self._transport, 'send_message'):
                await self._transport.send_message(navigation_message)
                logger.info(f"NavigationWebSocketEmitter: Sent navigation response via send_message: {response.get('type', 'unknown')}")
            else:
                logger.error("NavigationWebSocketEmitter: Transport has no send method available")
                
        except Exception as e:
            logger.error(f"NavigationWebSocketEmitter: Error emitting navigation response: {e}")
    
    async def emit_chart_focus_change(self, chart_id: str, chart_index: int, chart_title: str) -> None:
        """
        Emit a chart focus change event to the frontend.
        
        Args:
            chart_id: The ID of the focused chart
            chart_index: The index of the focused chart (0-based)
            chart_title: The title of the focused chart
        """
        try:
            focus_message = {
                "type": "chart-focus-changed",
                "data": {
                    "chart_id": chart_id,
                    "chart_index": chart_index,
                    "chart_title": chart_title,
                    "timestamp": str(int(time.time() * 1000)),
                    "source": "chart_navigator"
                }
            }
            
            await self.emit_navigation_response(focus_message["data"])
            
        except Exception as e:
            logger.error(f"NavigationWebSocketEmitter: Error emitting chart focus change: {e}")
    
    async def emit_navigation_error(self, error_message: str, original_command: str) -> None:
        """
        Emit a navigation error to the frontend.
        
        Args:
            error_message: The error message to display
            original_command: The original command that caused the error
        """
        try:
            error_response = {
                "type": "navigation_error",
                "error_message": error_message,
                "original_command": original_command,
                "timestamp": str(int(time.time() * 1000)),
                "source": "chart_navigator"
            }
            
            await self.emit_navigation_response(error_response)
            
        except Exception as e:
            logger.error(f"NavigationWebSocketEmitter: Error emitting navigation error: {e}")