"""
Chart Summarization Service using Azure OpenAI.
Combines multiple charts into a single summary chart with AI-generated insights.
"""

import json
import uuid
from typing import Dict, Any, List, Optional, Tuple
from app.core.logger import logger
from app.core import config
from openai import AsyncAzureOpenAI


class ChartSummarizationService:
    """
    AI-powered service for combining and summarizing multiple charts.
    
    Uses Azure OpenAI to analyze chart data and generate optimized summary visualizations.
    """
    
    def __init__(self):
        """Initialize the chart summarization service"""
        try:
            self.client = AsyncAzureOpenAI(
                api_key=config.AZURE_OPENAI_API_KEY,
                azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                api_version="2024-02-01"
            )
            self.model = config.AZURE_OPENAI_MODEL
            logger.info("ChartSummarizationService: Azure OpenAI client initialized successfully")
        except Exception as e:
            logger.warning(f"ChartSummarizationService: Failed to initialize Azure OpenAI client: {e}")
            self.client = None
    
    async def summarize_charts(
        self, 
        charts_data: List[Dict[str, Any]], 
        session_id: str,
        summary_type: str = "auto"
    ) -> Optional[Dict[str, Any]]:
        """
        Summarize multiple charts into a single chart.
        
        Args:
            charts_data: List of full chart data dictionaries
            session_id: Current session ID
            summary_type: Type of summary ("auto", "comparison", "trend", "aggregate")
            
        Returns:
            New chart data structure ready for UI generation, or None if failed
        """
        if not self.client or not charts_data:
            logger.warning("ChartSummarizationService: Client not available or no charts provided")
            return None
        
        try:
            # Extract chart information for AI analysis
            chart_info = self._extract_chart_info(charts_data)
            
            # Generate AI summary
            summary_response = await self._generate_chart_summary(chart_info, summary_type)
            
            if not summary_response:
                logger.error("ChartSummarizationService: Failed to generate AI summary")
                return None
            
            # Convert AI response to chart data structure
            chart_data = await self._convert_to_chart_data(summary_response, charts_data)
            
            if chart_data:
                chart_data["session_id"] = session_id
                chart_data["source_charts"] = [chart.get("index", i) for i, chart in enumerate(charts_data)]
                chart_data["is_summary"] = True
                logger.info(f"ChartSummarizationService: Successfully summarized {len(charts_data)} charts")
            
            return chart_data
            
        except Exception as e:
            logger.error(f"ChartSummarizationService: Error summarizing charts: {e}")
            return None
    
    def _extract_chart_info(self, charts_data: List[Dict[str, Any]]) -> str:
        """Extract key information from charts for AI analysis"""
        chart_summaries = []
        
        for i, chart in enumerate(charts_data, 1):
            props = chart.get("props", {})
            chart_type = chart.get("type", "unknown")
            title = props.get("title", f"Chart {i}")
            categories = props.get("categories", [])
            series = props.get("series", [])
            
            # Extract data points
            data_summary = []
            for series_item in series:
                series_name = series_item.get("name", "Data")
                series_data = series_item.get("data", [])
                data_summary.append(f"  - {series_name}: {series_data}")
            
            chart_summary = f"""
Chart {i} ({chart_type}):
- Title: {title}
- Categories: {categories}
- Data:
{chr(10).join(data_summary)}"""
            
            chart_summaries.append(chart_summary)
        
        return "\n".join(chart_summaries)
    
    async def _generate_chart_summary(self, chart_info: str, summary_type: str) -> Optional[Dict[str, Any]]:
        """Generate AI summary of charts"""
        try:
            system_prompt = self._get_summarization_prompt(summary_type)
            user_prompt = f"""
Analyze and summarize these charts:

{chart_info}

Generate a summary chart that combines this data meaningfully.
"""
            
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=1000
            )
            
            # Parse JSON response
            response_text = response.choices[0].message.content.strip()
            
            # Extract JSON from response
            import re
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group(1)
            
            return json.loads(response_text)
            
        except Exception as e:
            logger.error(f"ChartSummarizationService: Error generating AI summary: {e}")
            return None
    
    def _get_summarization_prompt(self, summary_type: str) -> str:
        """Get system prompt for chart summarization"""
        return f"""You are a data visualization expert. Your job is to analyze multiple charts and create a single summary chart that combines the data meaningfully.

Summary type: {summary_type}

Guidelines:
1. Choose the most appropriate chart type (bar, line, donut, or stat-card) for the combined data
2. Create meaningful categories and series that represent the combined insights
3. Generate a clear, descriptive title and subtitle
4. Provide a voice description that explains the summary insights
5. Ensure data is properly aggregated or compared as appropriate

IMPORTANT CONSTRAINTS:
- Bar charts: MUST have exactly ONE series only. Combine all data into a single series.
- Line charts: Can have multiple series for comparison
- Donut charts: Single data array, no series
- Stat cards: Single value

Response format (JSON only):
{{
  "chart_type": "bar" | "line" | "donut" | "stat-card",
  "title": "Summary chart title",
  "subtitle": "Optional subtitle",
  "categories": ["category1", "category2", ...],
  "series_data": [
    {{
      "name": "Series name",
      "data": [number, number, ...]
    }}
  ],
  "voice_description": "Detailed description of the summary insights",
  "summary_insights": "Key insights from combining the charts",
  "chart_specific_props": {{}}
}}

For bar charts, ensure series_data contains only ONE object with combined data.
Example for bar chart combining multiple datasets:
{{
  "chart_type": "bar",
  "categories": ["Category A", "Category B", "Category C"],
  "series_data": [
    {{
      "name": "Combined Data",
      "data": [total1, total2, total3]
    }}
  ]
}}

For donut charts, use:
- "data": [number, number, ...] instead of "series_data"
- "data_type": "percentage" | "currency" | "count"

For stat cards, use:
- "value": number
- "unit": "string"
- "trend": "up" | "down" | "neutral"

Always respond with valid JSON only."""
    
    async def _convert_to_chart_data(self, ai_response: Dict[str, Any], original_charts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Convert AI response to proper chart data structure"""
        try:
            chart_type = ai_response.get("chart_type")
            chart_id = f"summary_chart_{uuid.uuid4().hex[:8]}"
            
            # Base chart structure
            chart_data = {
                "id": chart_id,
                "type": f"{chart_type}-chart" if chart_type != "stat-card" else "single-stat-card",
                "props": {
                    "chartId": chart_id,
                    "title": ai_response.get("title", "Chart Summary"),
                    "subtitle": ai_response.get("subtitle"),
                }
            }
            
            # Add type-specific properties
            if chart_type == "donut":
                chart_data["props"].update({
                    "categories": ai_response.get("categories", []),
                    "series": [{"name": ai_response.get("title", "Summary"), "data": ai_response.get("data", [])}],
                    "colors": self._get_default_colors(len(ai_response.get("categories", []))),
                    "dataType": ai_response.get("data_type", "count"),
                    "totalValue": sum(ai_response.get("data", []))
                })
            elif chart_type == "stat-card":
                chart_data["props"].update({
                    "value": ai_response.get("value", 0),
                    "unit": ai_response.get("unit", ""),
                    "trend": ai_response.get("trend", "neutral")
                })
            else:  # bar or line chart
                chart_data["props"].update({
                    "categories": ai_response.get("categories", []),
                    "series": ai_response.get("series_data", [])
                })
            
            # Add summary metadata
            chart_data["props"]["summary_insights"] = ai_response.get("summary_insights", "")
            chart_data["props"]["is_ai_summary"] = True
            
            # Voice description
            voice_description = ai_response.get("voice_description", f"Summary of {len(original_charts)} charts")
            
            return {
                **chart_data,
                "voiceDescription": voice_description
            }
            
        except Exception as e:
            logger.error(f"ChartSummarizationService: Error converting AI response: {e}")
            return None
    
    def _get_default_colors(self, count: int) -> List[str]:
        """Get default colors for charts"""
        colors = [
            "#FF6B6B", "#4ECDC4", "#45B7D1", "#96CEB4", "#FFEAA7",
            "#DDA0DD", "#98D8C8", "#F7DC6F", "#BB8FCE", "#85C1E9"
        ]
        return colors[:count] if count <= len(colors) else colors * ((count // len(colors)) + 1)