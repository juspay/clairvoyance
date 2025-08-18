"""
UI Component types for chart generation via LLM function calling.
These types define the structure of chart data sent to the frontend.
"""

from typing import Dict, List, Optional, Any, Union
from pydantic import BaseModel
from datetime import datetime


class ChartSeries(BaseModel):
    """Data series for charts"""
    name: str
    data: List[Union[int, float]]
    color: Optional[str] = None


class ChartDataSpec(BaseModel):
    """Chart data specification matching frontend requirements"""
    title: str
    subtitle: Optional[str] = None
    categories: List[str]
    series: List[ChartSeries]
    changePercentages: Optional[List[float]] = None
    autoNarrate: bool = True
    interactive: bool = True
    metadata: Optional[Dict[str, Any]] = None


class UIComponentMetadata(BaseModel):
    """Metadata for UI components"""
    generatedAt: str
    dataSource: str
    confidence: float = 0.95


class UIComponentData(BaseModel):
    """Component data wrapper"""
    id: str
    metadata: UIComponentMetadata


class UIComponentEvent(BaseModel):
    """Complete UI component event structure for WebSocket emission"""
    status: str = "completed"
    message: str
    componentType: str  # "bar-chart", "line-chart", "donut-chart"
    data: ChartDataSpec
    voiceDescription: str
    renderOrder: int = 0
    componentData: UIComponentData