"""Shared Large Language Model (LLM) utilities for voice agents.

This package centralizes LLM integrations so that multiple voice agents can
reuse the same provider-specific setup logic.
"""

from __future__ import annotations

# Export all builder functions, config classes, and types
from .azure import AzureConfig, build_azure_llm
from .claude_vertex import ClaudeVertexConfig, build_claude_vertex_llm
from .types import LLMConfiguration, LLMProvider, LLMSdk, ThinkingConfiguration
from .vertex import VertexConfig, build_vertex_llm

__all__ = [
    # Types
    "LLMProvider",
    "LLMSdk",
    "LLMConfiguration",
    "ThinkingConfiguration",
    # Azure
    "AzureConfig",
    "build_azure_llm",
    # Google Vertex (Gemini)
    "VertexConfig",
    "build_vertex_llm",
    # Claude on Vertex
    "ClaudeVertexConfig",
    "build_claude_vertex_llm",
]
