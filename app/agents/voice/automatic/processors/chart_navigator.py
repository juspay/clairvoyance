"""
ChartNavigator - Enhanced navigation system for chart components.
Processes both simple navigation commands (next/previous) and complex LLM-powered
navigation (go to chart 3, show sales chart) when minimap is active.
"""

import re
import json
from typing import Optional, Dict, Any
from app.core.logger import logger
from pipecat.frames.frames import Frame, TranscriptionFrame, InterimTranscriptionFrame, UserStoppedSpeakingFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from app.agents.voice.automatic.features.charts.session_storage import get_session_storage
from app.agents.voice.automatic.utils.session_context import get_current_session_id
from app.agents.voice.automatic.features.charts.chart_tools import get_pending_chart_emissions
from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame


class ChartNavigator(FrameProcessor):
    """
    Enhanced chart navigation processor that handles both simple and complex navigation.
    
    Features:
    1. Simple navigation: next, previous, back commands (fast detection)
    2. Complex navigation: "go to chart 3", "show sales chart" (LLM-powered)
    3. Chart enumeration: "how many charts", "list charts"
    4. Semantic search: Navigate by chart content/title
    5. Chart summarization: "summarize chart 1 and 3", "combine charts" (AI-powered)
    6. WebSocket responses: Send navigation results to frontend
    
    Processing Flow:
    1. Fast detection for simple commands (next/prev)
    2. LLM processing for complex navigation requests
    3. Chart registry lookup for navigation targets
    4. WebSocket emission for frontend navigation
    """
    
    # Fast detection patterns - simple navigation commands
    FAST_NAVIGATION_PATTERNS = [
        r'\bnext\b',
        r'\bprevious\b', 
        r'\bprev\b',
        r'\bback\b',
        r'\bforward\b',
        r'\bbackward\b',
        r'\bsummarize\b',
        r'\bcombine\b',
        r'\bmerge\b'
    ]
    
    # Simple navigation command patterns (case-insensitive) 
    SIMPLE_NAVIGATION_PATTERNS = [
        r'\b(next|go\s+next|next\s+slide|show\s+next|next\s+chart)\b',
        r'\b(previous|prev|go\s+back|go\s+previous|back|previous\s+slide|show\s+previous|previous\s+chart)\b',
        r'\b(forward|move\s+forward)\b',
        r'\b(backward|move\s+backward)\b'
    ]
    
    # Complex navigation patterns that require LLM processing
    COMPLEX_NAVIGATION_PATTERNS = [
        r'\b(go\s+to\s+chart|show\s+chart|chart\s+number|chart\s+\d+)\b',
        r'\b(how\s+many\s+charts|list\s+charts|show\s+all\s+charts)\b',
        r'\b(show\s+.*chart|.*chart.*show|display.*chart)\b',
        r'\b(sales\s+chart|revenue\s+chart|.*\s+chart)\b',
        r'\b(summarize\s+chart|combine\s+chart|merge\s+chart|summary\s+of\s+chart)\b',
        r'\b(chart\s+\d+\s+and\s+chart\s+\d+|charts?\s+\d+.*\d+)\b'
    ]
    
    def __init__(self, name: str = "ChartNavigator", enable_fast_processing: bool = True, enable_llm_navigation: bool = True):
        """Initialize the chart navigator"""
        super().__init__(name=name)
        self._minimap_active = False
        self._fast_processing_enabled = enable_fast_processing
        self._llm_navigation_enabled = enable_llm_navigation
        self._simple_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.SIMPLE_NAVIGATION_PATTERNS]
        self._fast_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.FAST_NAVIGATION_PATTERNS]
        self._complex_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.COMPLEX_NAVIGATION_PATTERNS]
        self._navigation_handler = None  # Will be set externally
        logger.info(f"ChartNavigator initialized with {len(self.SIMPLE_NAVIGATION_PATTERNS)} simple patterns, {len(self.FAST_NAVIGATION_PATTERNS)} fast patterns, and {len(self.COMPLEX_NAVIGATION_PATTERNS)} complex patterns (fast: {enable_fast_processing}, llm: {enable_llm_navigation})")
    
    def set_minimap_active(self, active: bool) -> None:
        """Update the minimap active state"""
        if self._minimap_active != active:
            logger.info(f"ChartNavigator: minimap state changed to {'active' if active else 'inactive'}")
            self._minimap_active = active
    
    def is_minimap_active(self) -> bool:
        """Get current minimap state"""
        return self._minimap_active
    
    def _is_fast_navigation_command(self, text: str) -> bool:
        """
        Fast check for navigation keywords in partial transcriptions.
        
        Args:
            text: The transcribed text to analyze
            
        Returns:
            True if text contains fast navigation keywords, False otherwise
        """
        if not text or not text.strip():
            return False
        
        # Clean the text for better matching
        cleaned_text = text.strip().lower()
        
        # Check against fast patterns for immediate detection
        for pattern in self._fast_patterns:
            if pattern.search(cleaned_text):
                logger.info(f"Fast navigation command detected: '{text}' matches pattern: {pattern.pattern}")
                return True
        
        return False
    
    def set_navigation_handler(self, handler) -> None:
        """Set the navigation handler for processing commands"""
        self._navigation_handler = handler
    
    def _is_simple_navigation_command(self, text: str) -> bool:
        """
        Check if the given text contains simple navigation commands.
        
        Args:
            text: The transcribed text to analyze
            
        Returns:
            True if text contains simple navigation commands, False otherwise
        """
        if not text or not text.strip():
            return False
        
        # Clean the text for better matching
        cleaned_text = text.strip().lower()
        
        # Check against simple navigation patterns
        for pattern in self._simple_patterns:
            if pattern.search(cleaned_text):
                logger.debug(f"Simple navigation command detected: '{text}' matches pattern: {pattern.pattern}")
                return True
        
        return False
    
    def _is_complex_navigation_command(self, text: str) -> bool:
        """
        Check if the given text contains complex navigation commands that need LLM processing.
        
        Args:
            text: The transcribed text to analyze
            
        Returns:
            True if text contains complex navigation commands, False otherwise
        """
        if not text or not text.strip() or not self._llm_navigation_enabled:
            return False
        
        # Clean the text for better matching
        cleaned_text = text.strip().lower()
        
        # Check against complex navigation patterns
        for pattern in self._complex_patterns:
            if pattern.search(cleaned_text):
                logger.debug(f"Complex navigation command detected: '{text}' matches pattern: {pattern.pattern}")
                return True
        
        return False
    
    async def _handle_simple_navigation(self, text: str) -> bool:
        """
        Handle simple navigation commands (next, previous, etc.)
        
        Args:
            text: The navigation command text
            
        Returns:
            True if command was handled, False otherwise
        """
        cleaned_text = text.strip().lower()
        
        try:
            if any(pattern.search(cleaned_text) for pattern in self._simple_patterns):
                # Determine navigation direction
                direction = "next"
                if any(word in cleaned_text for word in ["previous", "prev", "back", "backward"]):
                    direction = "previous"
                
                # Send chart navigation to frontend (just the direction for minimap)
                await self._send_chart_navigation({
                    "action": "navigate",
                    "direction": direction,
                    "lastChart": False
                })
                
                logger.info(f"ChartNavigator: Handled simple navigation - {direction}")
                return True
        except Exception as e:
            logger.error(f"ChartNavigator: Error handling simple navigation: {e}")
        
        return False
    
    async def _handle_complex_navigation(self, text: str) -> bool:
        """
        Handle complex navigation commands using LLM processing.
        
        Args:
            text: The navigation command text
            
        Returns:
            True if command was handled, False otherwise
        """
        if not self._navigation_handler:
            logger.warning("ChartNavigator: No navigation handler set for complex commands")
            return False
        
        try:
            # Get session ID and available charts
            session_id = get_current_session_id()
            storage = get_session_storage()
            available_charts = storage.get_chart_registry(session_id)
            
            # Process with LLM navigation handler
            result = await self._navigation_handler.process_navigation_command(
                text, available_charts, session_id
            )
            
            if result:
                # Handle different result types
                result_type = result.get("type")
                
                if result_type == "summarize_charts":
                    # For summarization, trigger the LLM function directly
                    await self._handle_chart_summarization(result, session_id)
                    logger.info(f"ChartNavigator: Handled chart summarization")
                    return True
                else:
                    # Convert LLM result to simple chart navigation
                    navigation_data = self._convert_llm_result_to_navigation(result)
                    if navigation_data:
                        await self._send_chart_navigation(navigation_data)
                        logger.info(f"ChartNavigator: Handled complex navigation - {result.get('type', 'unknown')}")
                        return True
        except Exception as e:
            logger.error(f"ChartNavigator: Error handling complex navigation: {e}")
        
        return False
    
    async def _handle_chart_summarization(self, llm_result: Dict[str, Any], session_id: str) -> None:
        """
        Handle chart summarization by calling the LLM function directly.
        
        Args:
            llm_result: The LLM navigation result containing summarization parameters
            session_id: Current session ID
        """
        try:
            from app.agents.voice.automatic.tools.navigation.functions import summarize_charts
            from pipecat.services.llm_service import FunctionCallParams
            
            # Create function call parameters from LLM result
            chart_indices = llm_result.get("chart_indices", [])
            summary_type = llm_result.get("summary_type", "auto")
            
            # Create a mock FunctionCallParams object
            class MockParams:
                def __init__(self, arguments):
                    self.arguments = arguments
                    self._result = None
                
                async def result_callback(self, result):
                    self._result = result
                    logger.info(f"ChartNavigator: Summarization result: {result}")
            
            params = MockParams({
                "chart_indices": chart_indices,
                "summary_type": summary_type,
                "session_id": session_id
            })
            
            # Call the summarization function
            await summarize_charts(params)
            
            # First: Manually emit any pending chart components to frontend
            await self._emit_pending_charts(session_id)
            
            # Second: Get the specific chart ID from the pending emissions and navigate to it
            pending_charts = get_pending_chart_emissions(session_id)
            if pending_charts:
                # Get the last emitted chart (should be the summary chart)
                summary_chart = pending_charts[-1]
                chart_id = summary_chart.get('id')
                
                # Find this chart in the registry to get its correct index
                storage = get_session_storage()
                charts = storage.get_chart_registry(session_id)
                target_index = None
                
                for chart in charts:
                    if chart.get('id') == chart_id:
                        target_index = chart.get('index')
                        break
                
                if target_index is not None:
                    await self._send_chart_navigation({
                        "action": "navigate_to_chart",
                        "chart_index": target_index,
                        "chart_id": chart_id,
                        "lastChart": False
                    })
                    logger.info(f"ChartNavigator: Navigated to summary chart '{chart_id}' at index {target_index}")
                else:
                    logger.warning(f"ChartNavigator: Could not find summary chart '{chart_id}' in registry")
            
        except Exception as e:
            logger.error(f"ChartNavigator: Error handling chart summarization: {e}")
    
    async def _emit_pending_charts(self, session_id: str) -> None:
        """
        Manually emit pending chart components to frontend via RTVI.
        
        Args:
            session_id: Current session ID
        """
        try:
            if not self._rtvi_processor:
                logger.warning("ChartNavigator: No RTVI processor available for chart emission")
                return
            
            # Get pending chart emissions from chart_tools
            pending_charts = get_pending_chart_emissions(session_id)
            
            # Emit each chart component to frontend
            for chart_data in pending_charts:
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={"type": "ui-component", "payload": chart_data}
                    )
                )
                logger.info(f"ChartNavigator: Emitted chart component: {chart_data.get('id', 'unknown')}")
            
            if pending_charts:
                logger.info(f"ChartNavigator: Emitted {len(pending_charts)} chart components to frontend")
            
        except Exception as e:
            logger.error(f"ChartNavigator: Error emitting pending charts: {e}")
    
    def set_rtvi_processor(self, rtvi_processor) -> None:
        """Set the RTVI processor for sending chart navigation"""
        self._rtvi_processor = rtvi_processor
    
    def _convert_llm_result_to_navigation(self, llm_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert LLM navigation result to simple chart navigation data"""
        try:
            result_type = llm_result.get("type")
            
            if result_type == "navigate_to_chart":
                navigation_data = {
                    "action": "navigate_to_chart",
                    "chart_index": llm_result.get("target_chart_index"),
                    "chart_id": llm_result.get("target_chart_id"),
                    "lastChart": llm_result.get("lastChart", False)
                }
                return navigation_data
            elif result_type == "search_charts":
                # For search, navigate to first result if available
                if llm_result.get("matching_charts"):
                    first_match = llm_result["matching_charts"][0]
                    return {
                        "action": "navigate_to_chart", 
                        "chart_index": first_match.get("index"),
                        "chart_id": first_match.get("id"),
                        "lastChart": False
                    }
            
            return None
        except Exception as e:
            logger.error(f"ChartNavigator: Error converting LLM result: {e}")
            return None
    
    async def _send_chart_navigation(self, navigation_data: Dict[str, Any]) -> None:
        """
        Send chart navigation to frontend via RTVI for minimap display.
        
        Args:
            navigation_data: Simple navigation data (which chart to show)
        """
        try:
            # Send via RTVI if processor is available
            if hasattr(self, '_rtvi_processor') and self._rtvi_processor:
                from pipecat.processors.frameworks.rtvi import RTVIServerMessageFrame
                
                await self._rtvi_processor.push_frame(
                    RTVIServerMessageFrame(
                        data={
                            "type": "chart-navigation",
                            "payload": navigation_data
                        }
                    )
                )
                logger.info(f"ChartNavigator: Sent chart navigation via RTVI: {navigation_data.get('action', 'unknown')}")
            else:
                # Fallback: log for debugging
                logger.info(f"ChartNavigator: Chart navigation (no RTVI): {json.dumps(navigation_data, indent=2)}")
        except Exception as e:
            logger.error(f"ChartNavigator: Error sending chart navigation: {e}")
    
    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """
        Process frames and filter navigation commands when minimap is active.
        Fast detection on interim transcriptions to stop listening immediately.
        
        Args:
            frame: The frame to process
            direction: The direction of frame flow
        """
        # Always call super first
        await super().process_frame(frame, direction)

        # Handle chart navigation only when minimap is active
        if self._minimap_active:
            # Handle interim transcriptions for fast detection (if enabled)
            if isinstance(frame, InterimTranscriptionFrame) and self._fast_processing_enabled:
                if self._is_fast_navigation_command(frame.text):
                    logger.info(f"ChartNavigator: Fast navigation detected in interim: '{frame.text}' - stopping transcription")
                    # Immediately stop the user from speaking to process the command fast
                    await self.push_frame(UserStoppedSpeakingFrame(), direction)
                    # Convert interim to final transcription for immediate processing
                    final_frame = TranscriptionFrame(text=frame.text, user_id=frame.user_id, timestamp=frame.timestamp)
                    await self.push_frame(final_frame, direction)
                    return  # Don't push the interim frame
                else:
                    logger.debug(f"ChartNavigator: Non-navigation interim text: '{frame.text}'")
            
            # Handle final transcriptions  
            elif isinstance(frame, TranscriptionFrame):
                text = frame.text
                
                # Check for simple navigation first (faster)
                if self._is_simple_navigation_command(text):
                    logger.info(f"ChartNavigator: Processing simple navigation command: '{text}'")
                    await self._handle_simple_navigation(text)
                    return  # Don't pass to LLM
                
                # Check for complex navigation
                else:
                    logger.info(f"ChartNavigator: Processing complex navigation command: '{text}'")
                    handled = await self._handle_complex_navigation(text)
                    if handled:
                        return  # Don't pass to LLM
                
                # Not a navigation command, log and pass through
                logger.debug(f"ChartNavigator: Passing through non-navigation text '{text}' (minimap active)")

        if self._minimap_active:
            return
        else:
            await self.push_frame(frame, direction)
 
        # Pass frame through for all other cases:
        # - Not a TranscriptionFrame/InterimTranscriptionFrame
        # - Minimap not active
        # - TranscriptionFrame but not a navigation command
        