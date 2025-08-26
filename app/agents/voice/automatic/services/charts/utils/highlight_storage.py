"""
Shared storage for highlights between text filter and frame processor.
Allows HighlightedChartTextFilter to store highlights and TTSTimingProcessor to retrieve them.
"""

from typing import Dict, List, Any
from app.core.logger import logger

# Global storage for pending highlights by session
_session_highlights: Dict[str, List[Dict[str, Any]]] = {}


def store_highlights_for_session(session_id: str, highlights: List[Dict[str, Any]]):
    """Store highlights for a session."""
    global _session_highlights
    _session_highlights[session_id] = highlights
    logger.debug(f"[{session_id}] Stored {len(highlights)} highlights for timing correlation")


def get_highlights_for_session(session_id: str) -> List[Dict[str, Any]]:
    """Get highlights for a session."""
    global _session_highlights
    return _session_highlights.get(session_id, [])


def clear_highlights_for_session(session_id: str):
    """Clear highlights for a session."""
    global _session_highlights
    if session_id in _session_highlights:
        del _session_highlights[session_id]
        logger.debug(f"[{session_id}] Cleared session highlights")


def update_highlight_timestamp(session_id: str, trigger_word: str, timestamp_ms: float, word: str):
    """Update a specific highlight with precise timing."""
    global _session_highlights
    highlights = _session_highlights.get(session_id, [])
    
    for highlight in highlights:
        if highlight.get('timestamp') is None:  # Not yet triggered
            highlight_trigger = highlight.get('triggerWord', '').lower()
            
            if trigger_word.lower() == highlight_trigger:
                # Update with precise timing
                highlight['timestamp'] = int(timestamp_ms)
                highlight['precise_timestamp'] = timestamp_ms
                highlight['timing_source'] = 'elevenlabs_exact'
                highlight['actual_trigger_word'] = word
                
                logger.debug(f"[{session_id}] Updated highlight timing: {highlight['category']} at {timestamp_ms:.1f}ms")
                return highlight
    
    return None