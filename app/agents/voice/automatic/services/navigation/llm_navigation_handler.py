"""
LLM-powered navigation handler for complex chart navigation commands.
Processes natural language requests like "go to chart 3", "show sales chart", etc.
"""

import json
import re
from typing import Dict, Any, List, Optional
from app.core.logger import logger
from app.core import config
from openai import AsyncAzureOpenAI


class LLMNavigationHandler:
    """
    LLM-powered handler for complex chart navigation commands.
    
    Processes natural language navigation requests and returns structured
    responses for frontend navigation actions.
    """
    
    def __init__(self):
        """Initialize the LLM navigation handler"""
        try:
            self.client = AsyncAzureOpenAI(
                api_key=config.AZURE_OPENAI_API_KEY,
                azure_endpoint=config.AZURE_OPENAI_ENDPOINT,
                api_version="2024-02-01"
            )
            self.model = config.AZURE_OPENAI_MODEL
            logger.info("LLMNavigationHandler: Azure OpenAI client initialized successfully")
        except Exception as e:
            logger.warning(f"LLMNavigationHandler: Failed to initialize Azure OpenAI client: {e}, complex navigation disabled")
            self.client = None
    
    async def process_navigation_command(
        self, 
        command: str, 
        available_charts: List[Dict[str, Any]], 
        session_id: str
    ) -> Optional[Dict[str, Any]]:
        """
        Process a navigation command using LLM analysis.
        
        Args:
            command: The user's navigation command
            available_charts: List of available chart metadata
            session_id: Current session ID
            
        Returns:
            Navigation response dict or None if command couldn't be processed
        """
        if not self.client:
            logger.warning("LLMNavigationHandler: OpenAI client not available")
            return None
        
        try:
            # Create context about available charts
            charts_context = self._create_charts_context(available_charts)
            
            # Build the navigation prompt
            prompt = self._build_navigation_prompt(command, charts_context)
            
            # Get LLM response
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": self._get_system_prompt()
                    },
                    {
                        "role": "user", 
                        "content": prompt
                    }
                ],
                temperature=0.1,
                max_tokens=500
            )
            
            # Parse the response
            result = self._parse_llm_response(response.choices[0].message.content)
            
            if result:
                result["session_id"] = session_id
                result["original_command"] = command
                
                # If it's a search request, perform the actual search
                if result.get("type") == "search_charts" and result.get("search_query"):
                    from app.agents.voice.automatic.features.charts.session_storage import get_session_storage
                    storage = get_session_storage()
                    matching_charts = storage.search_charts(session_id, result["search_query"])
                    result["matching_charts"] = matching_charts
                    logger.info(f"LLMNavigationHandler: Found {len(matching_charts)} charts matching '{result['search_query']}'")
                
                logger.info(f"LLMNavigationHandler: Processed command '{command}' -> {result['type']}")
                return result
            
        except Exception as e:
            logger.error(f"LLMNavigationHandler: Error processing command '{command}': {e}")
        
        return None
    
    def _create_charts_context(self, charts: List[Dict[str, Any]]) -> str:
        """Create context string about available charts"""
        if not charts:
            return "No charts are currently available."
        
        context_lines = ["Available charts:"]
        for i, chart in enumerate(charts, 1):
            chart_info = f"{i}. {chart.get('title', 'Untitled Chart')}"
            if chart.get('type'):
                chart_info += f" ({chart['type']})"
            if chart.get('id'):
                chart_info += f" [ID: {chart['id']}]"
            context_lines.append(chart_info)
        
        return "\n".join(context_lines)
    
    def _build_navigation_prompt(self, command: str, charts_context: str) -> str:
        """Build the navigation prompt for the LLM"""
        return f"""
User navigation command: "{command}"

{charts_context}

Please analyze the user's navigation request and provide a structured response.
"""
    
    def _get_system_prompt(self) -> str:
        """Get the system prompt for navigation processing"""
        return """You are a chart navigation assistant. Your job is to interpret user navigation commands and return structured JSON responses.

IMPORTANT: When users refer to charts using ordinal numbers (first, second, third, etc.) or positional terms, convert them to 0-based indices:
- "first" = index 0
- "second" = index 1  
- "third" = index 2
- etc.

Supported navigation types:
1. "navigate_to_chart": Go to a specific chart by number or ID
2. "search_charts": Find charts by title, type, or content
3. "list_charts": Show information about available charts
4. "summarize_charts": Combine multiple charts into a summary chart
5. "navigation_error": When the request cannot be fulfilled

Response format (JSON only):
{
  "type": "navigate_to_chart" | "search_charts" | "list_charts" | "summarize_charts" | "navigation_error",
  "target_chart_index": number (0-based index, for navigate_to_chart),
  "target_chart_id": string (chart ID, for navigate_to_chart),
  "lastChart": boolean (true if navigating to last chart, false otherwise),
  "search_query": string (for search_charts),
  "chart_indices": array of numbers (0-based indices, for summarize_charts),
  "summary_type": string ("auto" | "comparison" | "trend" | "aggregate", for summarize_charts),
  "message": string (user-friendly response message),
  "error_reason": string (for navigation_error only)
}

Examples:
- "go to chart 3" -> {"type": "navigate_to_chart", "target_chart_index": 2, "lastChart": false, "message": "Navigating to chart 3"}
- "go to last chart" -> {"type": "navigate_to_chart", "lastChart": true, "message": "Navigating to last chart"}
- "show me the final chart" -> {"type": "navigate_to_chart", "lastChart": true, "message": "Navigating to last chart"}
- "go to the end" -> {"type": "navigate_to_chart", "lastChart": true, "message": "Navigating to last chart"}
- "show sales chart" -> {"type": "search_charts", "search_query": "sales", "message": "Searching for sales charts"}
- "how many charts are there" -> {"type": "list_charts", "message": "Showing available charts"}
- "summarize chart 1 and chart 3" -> {"type": "summarize_charts", "chart_indices": [0, 2], "summary_type": "auto", "message": "Creating summary of charts 1 and 3"}
- "combine charts 2, 4, and 5" -> {"type": "summarize_charts", "chart_indices": [1, 3, 4], "summary_type": "auto", "message": "Combining charts 2, 4, and 5"}
- "combine chart first and chart second" -> {"type": "summarize_charts", "chart_indices": [0, 1], "summary_type": "auto", "message": "Combining first and second charts"}
- "merge the first and third chart" -> {"type": "summarize_charts", "chart_indices": [0, 2], "summary_type": "auto", "message": "Merging first and third charts"}
- "combine all charts" -> {"type": "summarize_charts", "chart_indices": [0, 1], "summary_type": "auto", "message": "Combining all available charts"}
- "go to chart 10" (when only 3 charts exist) -> {"type": "navigation_error", "error_reason": "Chart 10 not found", "message": "Chart 10 doesn't exist. There are only 3 charts available."}

Always respond with valid JSON only, no additional text."""
    
    def _parse_llm_response(self, response_text: str) -> Optional[Dict[str, Any]]:
        """Parse the LLM response and validate the structure"""
        try:
            # Clean the response text
            response_text = response_text.strip()
            
            # Extract JSON if it's wrapped in markdown
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response_text, re.DOTALL)
            if json_match:
                response_text = json_match.group(1)
            
            # Parse JSON
            result = json.loads(response_text)
            
            # Validate required fields
            if not isinstance(result, dict) or "type" not in result:
                logger.error(f"LLMNavigationHandler: Invalid response structure: {result}")
                return None
            
            # Validate navigation type
            valid_types = ["navigate_to_chart", "search_charts", "list_charts", "summarize_charts", "navigation_error"]
            if result["type"] not in valid_types:
                logger.error(f"LLMNavigationHandler: Invalid navigation type: {result['type']}")
                return None
            
            return result
            
        except json.JSONDecodeError as e:
            logger.error(f"LLMNavigationHandler: Failed to parse JSON response: {e}")
            logger.debug(f"LLMNavigationHandler: Raw response: {response_text}")
            return None
        except Exception as e:
            logger.error(f"LLMNavigationHandler: Error parsing response: {e}")
            return None