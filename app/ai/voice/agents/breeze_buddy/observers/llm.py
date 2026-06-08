"""Compatibility import for the shared structured tool-call helper."""

from app.ai.voice.llm.tool_call import ToolCallResult, call_llm

__all__ = ["ToolCallResult", "call_llm"]
