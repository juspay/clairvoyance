import re
from typing import Any, Mapping

from pipecat.utils.text.base_text_filter import BaseTextFilter
from app.core.logger import logger


class HighlightedChartTextFilter(BaseTextFilter):
    """TTS text filter that removes highlight XML tags from text."""
    
    def __init__(self, session_id: str):
        super().__init__()
        self._highlight_pattern = re.compile(
            r'<highlight\s+category=["\']([^"\']+)["\'][^>]*>(.*?)</highlight>',
            re.IGNORECASE | re.DOTALL
        )
    
    async def filter(self, text: str) -> str:
        """Remove highlight XML tags from text, return clean text for TTS."""
        if not text:
            return text
        return self._remove_highlight_tags(text)
    
    def _remove_highlight_tags(self, text: str) -> str:
        """Remove all highlight XML tags from text, keeping only the inner content."""
        clean_text = self._highlight_pattern.sub(r'\2', text)
        return clean_text.strip()
    
    async def update_settings(self, settings: Mapping[str, Any]) -> None:
        """Update filter settings."""
        pass
    
    async def reset_interruption(self) -> None:
        """Reset filter state on interruption."""
        pass
    
    async def handle_interruption(self) -> None:
        """Handle filter interruption."""
        pass