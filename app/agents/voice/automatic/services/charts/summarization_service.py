"""
Chart Summarization Service using Azure OpenAI.
Combines multiple charts into a single summary chart with AI-generated insights.
"""

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import AsyncAzureOpenAI

from app.core import config
from app.core.logger import logger


class ChartSummarizationService:
    """Service for AI-powered chart summarization and combination"""

    def __init__(self, llm_service=None):
        """Initialize the chart summarization service

        Args:
            llm_service: Optional main pipeline LLM service to reuse.
                        If None, creates separate Azure OpenAI client.
        """
        self.llm_service = llm_service
        self._azure_client = None
        if not llm_service:
            self._initialize_azure_client()

    def _initialize_azure_client(self):
        """Initialize Azure OpenAI client"""
        try:
            if hasattr(config, "AZURE_OPENAI_API_KEY") and config.AZURE_OPENAI_API_KEY:
                self._azure_client = AsyncAzureOpenAI(
                    api_key=config.AZURE_OPENAI_API_KEY,
                    api_version=getattr(
                        config, "AZURE_OPENAI_API_VERSION", "2024-02-01"
                    ),
                    azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                )
                logger.info(
                    "ChartSummarizationService: Azure OpenAI client initialized"
                )
            else:
                logger.warning("ChartSummarizationService: Azure OpenAI not configured")
        except Exception as e:
            logger.error(
                f"ChartSummarizationService: Failed to initialize Azure client: {e}"
            )

    async def _call_main_llm_service(
        self,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 1500,
    ):
        """
        Call summarization through main pipeline LLM service.

        Args:
            messages: The messages to send to LLM
            model: Model name to use
            temperature: Sampling temperature
            max_tokens: Maximum tokens

        Returns:
            Response object compatible with Azure OpenAI format
        """
        try:
            # Use the main LLM service (which may be wrapped)
            if hasattr(self.llm_service, "service"):
                # If it's wrapped (LLMServiceWrapper), get the underlying service
                llm = self.llm_service.service
            else:
                # Direct service
                llm = self.llm_service

            # Call the LLM service using correct attributes
            logger.info(
                f"ChartSummarizationService: Calling main LLM with model: {llm.model_name}"
            )
            response = await llm._client.chat.completions.create(
                model=llm.model_name,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

            logger.info(
                "ChartSummarizationService: Successfully called main LLM service"
            )
            return response

        except Exception as e:
            logger.error(
                f"ChartSummarizationService: Error calling main LLM service: {e}"
            )
            raise

    async def summarize_charts(
        self,
        charts_data: List[Dict[str, Any]],
        session_id: str,
        summary_type: str = "auto",
    ) -> Optional[Dict[str, Any]]:
        """
        Combine multiple charts into a single AI-generated summary chart.

        Args:
            charts_data: List of chart data to summarize
            session_id: Current session ID
            summary_type: Type of summary ("auto", "comparison", "trend", "overview")

        Returns:
            Summary chart data or None if failed
        """
        try:
            logger.info(
                f"ChartSummarizationService: Summarizing {len(charts_data)} charts for session {session_id}"
            )

            # Prepare charts data for LLM
            charts_context = self._prepare_charts_context(charts_data)

            # Generate summary with LLM
            summary_prompt = self._create_summary_prompt(charts_context, summary_type)

            messages = [
                {"role": "system", "content": self._get_summary_system_prompt()},
                {"role": "user", "content": summary_prompt},
            ]

            # Try to use main pipeline LLM service first
            if self.llm_service:
                logger.info(
                    "ChartSummarizationService: Using main pipeline LLM service"
                )
                response = await self._call_main_llm_service(
                    messages=messages,
                    model=getattr(config, "AZURE_OPENAI_MODEL_NAME", "gpt-4o"),
                    temperature=0.3,
                    max_tokens=1500,
                )
            else:
                # Fallback to separate Azure client
                logger.info("ChartSummarizationService: Using separate Azure client")
                if not self._azure_client:
                    logger.warning(
                        "ChartSummarizationService: Azure client not available"
                    )
                    return None

                response = await self._azure_client.chat.completions.create(
                    model=getattr(config, "AZURE_OPENAI_MODEL_NAME", "gpt-4o"),
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1500,
                )

            # Parse LLM response into chart format
            summary_result = self._parse_summary_response(
                response.choices[0].message.content
            )

            if summary_result:
                # Generate unique chart ID
                chart_id = f"summary_chart_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

                # Create final summary chart
                summary_chart = {
                    "id": chart_id,
                    "type": summary_result.get("chart_type", "bar-chart"),
                    "props": {
                        "chartId": chart_id,
                        "title": summary_result.get("title", "Chart Summary"),
                        "subtitle": summary_result.get("subtitle", ""),
                        **summary_result.get("props", {}),
                    },
                    "voiceDescription": summary_result.get("voice_description", ""),
                    "renderOrder": 0,
                    "metadata": {
                        "generatedAt": datetime.now().isoformat(),
                        "chartType": "chart",
                        "confidence": 0.95,
                        "status": "completed",
                        "is_summary": True,
                        "source_chart_count": len(charts_data),
                        "summary_type": summary_type,
                    },
                    "uiComponent": True,
                }

                logger.info(
                    f"ChartSummarizationService: Successfully created summary chart for session {session_id}"
                )
                return summary_chart
            else:
                logger.error(
                    "ChartSummarizationService: Failed to parse LLM summary response"
                )
                return None

        except Exception as e:
            logger.error(f"ChartSummarizationService: Error creating summary: {e}")
            return None

    def _prepare_charts_context(self, charts_data: List[Dict[str, Any]]) -> str:
        """Prepare charts data context for LLM"""
        charts_context = []

        for i, chart_data in enumerate(charts_data):
            props = chart_data.get("props", {})
            chart_info = {
                "index": i + 1,
                "title": props.get("title", "Untitled Chart"),
                "type": chart_data.get("type", "unknown"),
                "categories": props.get("categories", []),
                "series": props.get("series", []),
                "voice_description": chart_data.get("voiceDescription", ""),
            }

            # Debug logging to see what data we're actually getting
            logger.info(
                f"🔍 Summarization: Chart {i+1} '{chart_info['title']}' has {len(chart_info['categories'])} categories and {len(chart_info['series'])} series"
            )
            if chart_info["series"]:
                for j, series in enumerate(chart_info["series"]):
                    logger.info(
                        f"🔍 Summarization: Series {j+1}: '{series.get('name', 'Unnamed')}' with {len(series.get('data', []))} data points"
                    )

            charts_context.append(f"Chart {i + 1}: {json.dumps(chart_info, indent=2)}")

        return "\n\n".join(charts_context)

    def _get_summary_system_prompt(self) -> str:
        """Get system prompt for chart summarization"""
        return """You are an expert data analyst. Your job is to analyze multiple charts and create a single, insightful summary chart that combines the key insights.

ANALYSIS APPROACH:
1. Identify common themes, trends, and relationships across charts
2. Determine the best chart type for the combined insights
3. Create meaningful categories and data series
4. Write a clear voice description explaining the insights

RESPONSE FORMAT (JSON only):
{
    "chart_type": "bar-chart|line-chart|donut-chart",
    "title": "Summary Chart Title",
    "subtitle": "Optional subtitle",
    "voice_description": "Clear explanation of insights and key findings",
    "props": {
        "categories": ["Cat1", "Cat2", ...],
        "series": [{"name": "Series1", "data": [1, 2, 3, ...]}],
        "data": [1, 2, 3, ...],  // For donut charts
        "data_type": "currency|numericalValue|percentage"  // For donut charts
    }
}

GUIDELINES:
- Focus on the most important insights
- Use clear, business-friendly language
- Ensure data makes sense for the chosen chart type
- Voice description should explain "what this means" not just "what the data shows"
"""

    def _create_summary_prompt(self, charts_context: str, summary_type: str) -> str:
        """Create prompt for chart summarization"""
        type_guidance = {
            "auto": "Choose the best approach based on the data",
            "comparison": "Focus on comparing values across different categories",
            "trend": "Emphasize trends and changes over time",
            "overview": "Provide a high-level overview of all key metrics",
        }

        guidance = type_guidance.get(summary_type, type_guidance["auto"])

        return f"""Analyze these charts and create a single summary chart that captures the key insights:

{charts_context}

Summary type requested: {summary_type} - {guidance}

Create a summary chart that combines the most important insights from these charts. Focus on what the business user needs to know."""

    def _parse_summary_response(self, response: str) -> Optional[Dict[str, Any]]:
        """Parse LLM summary response"""
        try:
            # Clean response
            response = response.strip()
            if response.startswith("```json"):
                response = response[7:]
            if response.endswith("```"):
                response = response[:-3]
            response = response.strip()

            # Parse JSON
            result = json.loads(response)

            # Validate required fields
            required_fields = ["chart_type", "title", "voice_description", "props"]
            if not all(field in result for field in required_fields):
                logger.error(
                    "ChartSummarizationService: Missing required fields in LLM response"
                )
                return None

            return result

        except (json.JSONDecodeError, KeyError) as e:
            logger.error(
                f"ChartSummarizationService: Failed to parse LLM response: {e}"
            )
            logger.error(f"ChartSummarizationService: Raw response: {response}")
            return None
