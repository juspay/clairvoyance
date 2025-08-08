"""
Chart generation tools for LLM function calling.
The LLM can call these tools to generate interactive charts with voice narration.
"""

import json
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.core.logger import logger
from app.types.ui_components import UIComponentEvent, UIComponentData, UIComponentMetadata, ChartDataSpec, ChartSeries

# Global registry for pending chart emissions
_pending_chart_emissions: Dict[str, List[Dict[str, Any]]] = {}

# Global registry for chart context (categories mapping for highlights)
_chart_contexts: Dict[str, Dict[str, Any]] = {}


def generate_bar_chart(
    title: str,
    categories: List[str],
    series_data: List[Dict[str, Any]],
    voice_description: str,
    subtitle: Optional[str] = None,
    change_percentages: Optional[List[float]] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Generate a bar chart component for comparative data analysis.
    
    Args:
        title: Chart title (e.g., "Payment Method Success Rates")
        categories: List of category labels (e.g., ["WALLET", "CARD", "UPI"])
        series_data: List of data series, each with 'name' and 'data' fields
                    Example: [{"name": "Success Rate (%)", "data": [66.67, 78.95, 53.92]}]
        voice_description: Natural language description for accessibility and narration
        subtitle: Optional chart subtitle
        change_percentages: Optional percentage changes for each category
        session_id: Session identifier for logging
        
    Returns:
        Success message for LLM
    """
    try:
        # Create chart series objects
        series = []
        for s in series_data:
            series.append(ChartSeries(
                name=s.get('name', 'Data'),
                data=s.get('data', []),
                color=s.get('color', '#1f77b4')
            ))
        
        # Create chart specification
        chart_spec = ChartDataSpec(
            title=title,
            subtitle=subtitle,
            categories=categories,
            series=series,
            changePercentages=change_percentages,
            autoNarrate=True,
            interactive=True,
            metadata={
                "chartType": "comparison",
                "sourceTime": datetime.now().isoformat(),
                "generatedBy": "llm_function_call"
            }
        )
        
        # Generate component ID
        component_id = f"bar_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Create UI component event
        ui_component = UIComponentEvent(
            status="completed",
            message="Generated bar chart visualization",
            componentType="bar-chart",
            data=chart_spec,
            voiceDescription=voice_description,
            renderOrder=0,
            componentData=UIComponentData(
                id=component_id,
                metadata=UIComponentMetadata(
                    generatedAt=datetime.now().isoformat(),
                    dataSource="llm_generated",
                    confidence=0.95
                )
            )
        )
        
        # Store component for WebSocket emission
        _store_ui_component(ui_component, session_id)
        
        logger.info(f"[{session_id}] Generated bar chart: {title} with {len(categories)} categories")
        return f"Successfully generated bar chart '{title}' with {len(categories)} categories and {len(series)} data series."
        
    except Exception as e:
        logger.error(f"[{session_id}] Error generating bar chart: {e}")
        return f"Error generating bar chart: {str(e)}"


def generate_line_chart(
    title: str,
    categories: List[str],
    series_data: List[Dict[str, Any]],
    voice_description: str,
    subtitle: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Generate a line chart component for time series or trend analysis.
    
    Args:
        title: Chart title (e.g., "Sales Trend Over Time")
        categories: List of time/category labels (e.g., ["Jan", "Feb", "Mar"])
        series_data: List of data series for trend lines
        voice_description: Natural language description for accessibility
        subtitle: Optional chart subtitle
        session_id: Session identifier for logging
        
    Returns:
        Success message for LLM
    """
    try:
        # Create chart series objects
        series = []
        for s in series_data:
            series.append(ChartSeries(
                name=s.get('name', 'Trend'),
                data=s.get('data', []),
                color=s.get('color', '#ff7f0e')
            ))
        
        # Create chart specification
        chart_spec = ChartDataSpec(
            title=title,
            subtitle=subtitle,
            categories=categories,
            series=series,
            autoNarrate=True,
            interactive=True,
            metadata={
                "chartType": "trend",
                "sourceTime": datetime.now().isoformat(),
                "generatedBy": "llm_function_call"
            }
        )
        
        # Generate component ID
        component_id = f"line_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Create UI component event
        ui_component = UIComponentEvent(
            status="completed",
            message="Generated line chart visualization",
            componentType="line-chart",
            data=chart_spec,
            voiceDescription=voice_description,
            renderOrder=0,
            componentData=UIComponentData(
                id=component_id,
                metadata=UIComponentMetadata(
                    generatedAt=datetime.now().isoformat(),
                    dataSource="llm_generated",
                    confidence=0.95
                )
            )
        )
        
        # Store component for WebSocket emission
        _store_ui_component(ui_component, session_id)
        
        logger.info(f"[{session_id}] Generated line chart: {title} with {len(categories)} time points")
        return f"Successfully generated line chart '{title}' showing trends across {len(categories)} time points."
        
    except Exception as e:
        logger.error(f"[{session_id}] Error generating line chart: {e}")
        return f"Error generating line chart: {str(e)}"


def generate_donut_chart(
    title: str,
    categories: List[str],
    data: List[float],
    voice_description: str,
    subtitle: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Generate a donut chart component for percentage/proportion analysis.
    
    Args:
        title: Chart title (e.g., "Payment Method Distribution")
        categories: List of category labels (e.g., ["Credit Card", "UPI", "Wallet"])
        data: List of values/percentages for each category
        voice_description: Natural language description for accessibility
        subtitle: Optional chart subtitle
        session_id: Session identifier for logging
        
    Returns:
        Success message for LLM
    """
    try:
        # Create single series for donut chart
        series = [ChartSeries(
            name="Distribution",
            data=data,
            color="#2ca02c"
        )]
        
        # Create chart specification
        chart_spec = ChartDataSpec(
            title=title,
            subtitle=subtitle,
            categories=categories,
            series=series,
            autoNarrate=True,
            interactive=True,
            metadata={
                "chartType": "distribution",
                "sourceTime": datetime.now().isoformat(),
                "generatedBy": "llm_function_call"
            }
        )
        
        # Generate component ID
        component_id = f"donut_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # Create UI component event
        ui_component = UIComponentEvent(
            status="completed",
            message="Generated donut chart visualization",
            componentType="donut-chart",
            data=chart_spec,
            voiceDescription=voice_description,
            renderOrder=0,
            componentData=UIComponentData(
                id=component_id,
                metadata=UIComponentMetadata(
                    generatedAt=datetime.now().isoformat(),
                    dataSource="llm_generated",
                    confidence=0.95
                )
            )
        )
        
        # Store component for WebSocket emission
        _store_ui_component(ui_component, session_id)
        
        logger.info(f"[{session_id}] Generated donut chart: {title} with {len(categories)} segments")
        return f"Successfully generated donut chart '{title}' showing distribution across {len(categories)} categories."
        
    except Exception as e:
        logger.error(f"[{session_id}] Error generating donut chart: {e}")
        return f"Error generating donut chart: {str(e)}"


def _store_ui_component(ui_component: UIComponentEvent, session_id: Optional[str]):
    """
    Emit UI component via RTVI frame for voice agent system.
    This follows the same pattern as LLMSpyProcessor for tool call events.
    """
    if not session_id:
        logger.warning("No session_id provided for UI component emission")
        return
    
    try:
        # Import here to avoid circular imports
        from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
        import time
        
        # Create chart component data for frontend
        component_data = {
            "componentId": ui_component.componentData.id,
            "componentType": ui_component.componentType,
            "status": ui_component.status,
            "message": ui_component.message,
            "data": {
                "title": ui_component.data.title,
                "subtitle": ui_component.data.subtitle,
                "categories": ui_component.data.categories,
                "series": [{"name": s.name, "data": s.data, "color": s.color} for s in ui_component.data.series],
                "metadata": ui_component.data.metadata
            },
            "voiceDescription": ui_component.voiceDescription,
            "renderOrder": ui_component.renderOrder
        }
        
        # Log detailed information about the UI component event
        logger.info(f"[{session_id}] 📊 CHART EVENT EMITTED:")
        logger.info(f"[{session_id}] - Component ID: {ui_component.componentData.id}")
        logger.info(f"[{session_id}] - Component Type: {ui_component.componentType}")
        logger.info(f"[{session_id}] - Status: {ui_component.status}")
        logger.info(f"[{session_id}] - Message: {ui_component.message}")
        logger.info(f"[{session_id}] - Chart Title: {ui_component.data.title}")
        logger.info(f"[{session_id}] - Categories: {ui_component.data.categories}")
        logger.info(f"[{session_id}] - Series Count: {len(ui_component.data.series)}")
        logger.info(f"[{session_id}] - Voice Description: {ui_component.voiceDescription}")
        logger.info(f"[{session_id}] - Generated At: {ui_component.componentData.metadata.generatedAt}")
        logger.info(f"[{session_id}] - Chart Metadata: {ui_component.data.metadata}")
        logger.info(f"[{session_id}] 📋 FULL CHART EVENT JSON: {json.dumps(component_data, indent=2)}")
        
        # Store chart context for AI highlighting
        _store_chart_context(ui_component.componentData.id, {
            "categories": ui_component.data.categories,
            "chartType": ui_component.componentType,
            "title": ui_component.data.title,
            "chartId": ui_component.componentData.id,
            "sessionId": session_id
        })
        
        # Store in global registry to be picked up by RTVI emission
        _register_pending_chart_emission(session_id, component_data)
        
    except Exception as e:
        logger.error(f"[{session_id}] Failed to prepare UI component for emission: {e}")


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


def extract_highlights_from_text(text: str, chart_context: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract highlight information from AI response text based on chart context.
    Maps mentioned categories to their indices for frontend highlighting.
    """
    highlights = []
    
    if not chart_context or not chart_context.get('categories'):
        return highlights
    
    categories = chart_context['categories']
    chart_id = chart_context.get('chartId', 'unknown')
    
    # Create mapping for case-insensitive matching
    category_mapping = {}
    for i, category in enumerate(categories):
        category_mapping[category.lower()] = {
            'index': i,
            'original': category
        }
    
    # Common month mappings for time-based charts
    month_mapping = {
        'january': 'jan', 'february': 'feb', 'march': 'mar', 'april': 'apr',
        'may': 'may', 'june': 'jun', 'july': 'jul', 'august': 'aug',
        'september': 'sep', 'october': 'oct', 'november': 'nov', 'december': 'dec'
    }
    
    # Split text into words and check for category matches
    words = text.lower().replace(',', ' ').replace('.', ' ').split()
    
    for word in words:
        # Direct category match
        if word in category_mapping:
            highlights.append({
                'text': category_mapping[word]['original'],
                'categoryIndex': category_mapping[word]['index'],
                'type': 'category',
                'chartId': chart_id
            })
        
        # Month name variations
        elif word in month_mapping:
            short_month = month_mapping[word]
            if short_month in category_mapping:
                highlights.append({
                    'text': category_mapping[short_month]['original'],
                    'categoryIndex': category_mapping[short_month]['index'],
                    'type': 'category',
                    'chartId': chart_id
                })
    
    # Remove duplicates while preserving order
    seen = set()
    unique_highlights = []
    for highlight in highlights:
        key = (highlight['categoryIndex'], highlight['chartId'])
        if key not in seen:
            seen.add(key)
            unique_highlights.append(highlight)
    
    return unique_highlights