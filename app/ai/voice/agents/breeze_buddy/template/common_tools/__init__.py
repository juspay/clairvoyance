"""
Common Tools Package

Provides reusable tool categories (BASIC, MATH, STRING, CALL, EXTERNAL)
that can be included in templates via the `common_tools` configuration.

Usage in template JSON:
    {
        "configurations": {
            "common_tools": {"BASIC": ["*"]}
        }
    }

The value maps a category name to a list of node names:
    ["*"]            — available on all nodes (global function)
    ["initial"]      — available only on the "initial" node

Available categories:
    - BASIC: date, time, general utilities
    - MATH: calculations, conversions (future)
    - STRING: spell out, format text (future)
    - CALL: call duration, metadata (future)
    - EXTERNAL: API wrappers (future)
"""

from app.ai.voice.agents.breeze_buddy.template.common_tools.basic_tools import (
    register_basic_tools,
)
from app.ai.voice.agents.breeze_buddy.template.common_tools.registry import (
    CommonTool,
    CommonToolRegistry,
    ToolCategory,
)

__all__ = [
    "CommonTool",
    "CommonToolRegistry",
    "ToolCategory",
    "register_basic_tools",
]

# Auto-register basic tools when package is imported
register_basic_tools()
