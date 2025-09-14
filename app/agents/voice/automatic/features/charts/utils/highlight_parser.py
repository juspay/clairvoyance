"""
Highlight parser for AI-generated text with chart highlighting.
Parses highlight XML tags and correlates them with chart context.
"""

import re
from typing import List, Dict, Any, Tuple, Optional
from app.core.logger import logger


class HighlightTagParser:
    """Parser for highlight XML tags in AI responses"""

    def __init__(self):
        # Updated to match MCP regex exactly
        self.highlight_pattern = re.compile(
            r'<highlight\s+category=["\']([^"\']+)["\']>([^<]+)</highlight>',
            re.IGNORECASE,
        )

    def parse_highlight_tags(
        self, text: str, chart_context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Parse highlight tags from text and return clean text plus highlight metadata.

        Args:
            text: Text containing highlight tags
            chart_context: Chart context for mapping highlights to chart elements

        Returns:
            Dict with originalText, cleanText, and highlights
        """
        highlights = []
        clean_text = text

        # Find all highlight matches
        matches = list(self.highlight_pattern.finditer(text))

        # Process matches in reverse order to preserve text positions
        for match in reversed(matches):
            category = match.group(1)
            spoken_text = match.group(2).strip()

            # Create highlight metadata
            highlight = {
                "category": category,
                "spokenText": spoken_text,
                "type": "chart_category",
            }

            # Add chart context if available
            if chart_context:
                highlight["chartId"] = chart_context.get("chartId")
                highlight["chartType"] = chart_context.get("chartType")

                # Map category to chart index if categories are available
                categories = chart_context.get("categories", [])
                if categories:
                    try:
                        # Try exact match first
                        if category in categories:
                            highlight["categoryIndex"] = categories.index(category)
                        else:
                            # Try case-insensitive match
                            category_lower = category.lower()
                            for i, cat in enumerate(categories):
                                if cat.lower() == category_lower:
                                    highlight["categoryIndex"] = i
                                    break
                    except ValueError:
                        # Category not found in chart
                        highlight["categoryIndex"] = None

            highlights.insert(0, highlight)  # Insert at beginning to maintain order

            # Remove the highlight tag from text
            clean_text = (
                clean_text[: match.start()] + spoken_text + clean_text[match.end() :]
            )

        # Clean up any remaining XML tags and normalize spaces
        clean_text = re.sub(r"<[^>]+>", "", clean_text)
        clean_text = re.sub(r"\\s+", " ", clean_text).strip()

        return {"originalText": text, "cleanText": clean_text, "highlights": highlights}
