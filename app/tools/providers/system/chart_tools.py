"""
Chart generation tools for LLM function calling.
The LLM can call these tools to generate interactive charts with voice narration.
"""

import json
import time
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.core.logger import logger
from app.types.ui_components import UIComponentEvent, UIComponentData, UIComponentMetadata, ChartDataSpec, ChartSeries

# Global registry for pending chart emissions
_pending_chart_emissions: Dict[str, List[Dict[str, Any]]] = {}

# Global registry for chart context (categories mapping for highlights)
_chart_contexts: Dict[str, Dict[str, Any]] = {}

def get_pending_ui_components(session_id: str) -> List[UIComponentEvent]:
    """
    Retrieve and clear pending UI components for a session.
    Called by WebSocket handler to get components to emit.
    """
    try:
        from app.core.session_storage import get_session_storage
        storage = get_session_storage()
        return storage.get_pending_ui_components(session_id)
    except Exception as e:
        logger.error(f"[{session_id}] Failed to retrieve UI components: {e}")
        return []


def _register_pending_chart_emission(session_id: str, component_data: Dict[str, Any]):
    """Register a chart component for RTVI emission"""
    global _pending_chart_emissions
    if session_id not in _pending_chart_emissions:
        _pending_chart_emissions[session_id] = []
    _pending_chart_emissions[session_id].append(component_data)
    logger.debug(f"[{session_id}] Registered chart for RTVI emission: {component_data['componentId']}")


def get_pending_chart_emissions(session_id: str) -> List[Dict[str, Any]]:
    """Get and clear pending chart emissions for a session"""
    global _pending_chart_emissions
    charts = _pending_chart_emissions.get(session_id, [])
    if session_id in _pending_chart_emissions:
        del _pending_chart_emissions[session_id]
    return charts


def _store_chart_context(chart_id: str, context: Dict[str, Any]):
    """Store chart context for AI highlighting"""
    global _chart_contexts
    _chart_contexts[chart_id] = context
    logger.debug(f"Stored chart context for {chart_id}: {len(context.get('categories', []))} categories")


def get_latest_chart_context(session_id: str) -> Optional[Dict[str, Any]]:
    """Get the most recent chart context for a session"""
    global _chart_contexts
    
    # Find the most recent chart for this session
    session_charts = {
        chart_id: ctx for chart_id, ctx in _chart_contexts.items() 
        if ctx.get('sessionId') == session_id
    }
    
    if not session_charts:
        return None
    
    # Return the most recent one (charts are created with timestamps in IDs)
    latest_chart_id = max(session_charts.keys())
    return session_charts[latest_chart_id]
