"""
Highlight text filter for TTS that extracts XML highlight tags and prepares them for word timing correlation.
"""

import re
import time
from typing import Any, Mapping, Optional, List, Dict

from pipecat.utils.text.base_text_filter import BaseTextFilter

from app.core.logger import logger
from app.agents.voice.automatic.services.charts.utils.highlight_storage import store_highlights_for_session


class HighlightedChartTextFilter(BaseTextFilter):
    """
    TTS text filter that extracts highlight XML tags and prepares them for word timing correlation.
    
    Processes text like: "Credit cards <highlight category='Credit Card'>are crushing it</highlight> at 78%"
    - Extracts highlight information and stores for word timing correlation
    - Returns clean text: "Credit cards are crushing it at 78%" for TTS synthesis  
    - Correlates with TTSTextFrame from ElevenLabs for precise word timing
    - Emits highlights exactly when trigger words are spoken
    """
    
    def __init__(self, session_id: str):
        """
        Initialize highlight text filter.
        
        Args:
            session_id: Session identifier for context and correlation
        """
        super().__init__()
        self._session_id = session_id
        
        # XML pattern to match highlight tags
        self._highlight_pattern = re.compile(
            r'<highlight\s+category=["\']([^"\']+)["\'][^>]*>(.*?)</highlight>',
            re.IGNORECASE | re.DOTALL
        )
        
        logger.debug(f"HighlightedChartTextFilter initialized for session {session_id}")
    
    async def filter(self, text: str) -> str:
        """
        Extract highlights from text and store for word timing correlation, return clean text for TTS.
        
        Args:
            text: Input text with potential highlight XML tags
            
        Returns:
            Clean text without XML tags for TTS synthesis
        """
        if not text:
            return text
            
        try:
            # Extract highlight information
            highlights = self._extract_highlights(text)
            
            if highlights:
                # Store highlights for later TTSTextFrame correlation
                self._store_highlights(highlights)
                logger.debug(f"Extracted {len(highlights)} highlights for session {self._session_id}")
            
            # Return clean text without XML tags
            return self._remove_highlight_tags(text)
            
        except Exception as e:
            logger.error(f"Error in highlight filter for session {self._session_id}: {e}")
            # Return original text on error to avoid breaking TTS
            return text
    
    def _store_highlights(self, highlights: List[Dict[str, Any]]) -> None:
        """Store highlights for later word timing correlation."""
        try:
            
            store_highlights_for_session(self._session_id, highlights)
        except ImportError:
            logger.warning("Highlight storage not available - highlights will not be correlated with timing")
    
    def _extract_highlights(self, text: str) -> List[Dict[str, Any]]:
        """Extract highlight information from XML tags in text."""
        highlights = []
        
        for match in self._highlight_pattern.finditer(text):
            category = match.group(1).strip()
            spoken_text = match.group(2).strip()
            
            if not category or not spoken_text:
                continue
            
            # Create highlight data with chart context if available
            highlight_data = self._create_highlight_data(category, spoken_text)
            highlights.append(highlight_data)
        
        return highlights
    
    def _create_highlight_data(self, category: str, spoken_text: str) -> Dict[str, Any]:
        """Create highlight data structure with chart context."""
        # Get chart context for validation
        chart_context = self._get_latest_chart_context()
        
        # Base highlight data
        highlight_data = {
            'category': category,
            'spokenText': spoken_text,
            'created_at': int(time.time() * 1000),
            'timestamp': None,  # Will be set when first word is spoken
            'triggerWord': self._extract_trigger_word(spoken_text)
        }
        
        # Add chart context if available
        if chart_context:
            categories = chart_context.get('categories', [])
            if category in categories:
                highlight_data.update({
                    'categoryIndex': categories.index(category),
                    'chartId': chart_context.get('chartId', 'unknown')
                })
            else:
                # Category not in stored context - let frontend resolve
                highlight_data.update({
                    'categoryIndex': -1,
                    'chartId': chart_context.get('chartId', 'latest')
                })
                logger.debug(f"Category '{category}' not in chart context, using fallback")
        else:
            # No chart context available
            highlight_data.update({
                'categoryIndex': -1,
                'chartId': 'latest'
            })
        
        return highlight_data
    
    def _extract_trigger_word(self, spoken_text: str) -> str:
        """Extract the first word from spoken text as trigger word."""
        words = spoken_text.split()
        return words[0] if words else spoken_text
    
    def _remove_highlight_tags(self, text: str) -> str:
        """Remove all highlight XML tags from text, keeping only the inner content."""
        clean_text = self._highlight_pattern.sub(r'\2', text)
        return clean_text.strip()
    
    def _get_latest_chart_context(self) -> Optional[Dict[str, Any]]:
        """Get the most recent chart context for validation."""
        try:
            
            return get_latest_chart_context(self._session_id)
        except (ImportError, AttributeError):
            logger.debug("Chart tools not available - highlights will not have chart context")
            return None
        except Exception as e:
            logger.warning(f"Error getting chart context for session {self._session_id}: {e}")
            return None
    
    # Required by BaseTextFilter interface
    async def update_settings(self, settings: Mapping[str, Any]) -> None:
        """Update filter settings (no settings needed for highlight filter)."""
        pass
    
    async def reset_interruption(self) -> None:
        """Reset filter state on interruption."""
        pass
    
    async def handle_interruption(self) -> None:
        """Handle filter interruption."""
        pass