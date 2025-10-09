"""
Text Automatic Agent

A text-only agent that reuses the voice agent's tools and prompts infrastructure
for analytics, search, and other business logic without voice/audio complexity.

This module provides a clean, organized structure following the same patterns
as the voice agent but optimized for text-only interactions.
"""

from app.agents.voice.automatic.tools import initialize_tools

from .features import TextPipelineManager

# Global pipeline manager instance
pipeline_manager = TextPipelineManager()

__all__ = [
    "TextPipelineManager",
    "pipeline_manager",
    "initialize_tools",
]
