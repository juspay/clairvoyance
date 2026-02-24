"""
Common Tool Registry

Core registry for managing common tools organized by category.
Tools are registered once and can be retrieved filtered by category.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pipecat_flows import FlowsFunctionSchema

from app.core.logger import logger


class ToolCategory(str, Enum):
    """
    Categories for organizing common tools.

    Usage in template JSON:
        "common_tools": {"BASIC": ["*"], "CALL": ["initial"]}
    """

    BASIC = "BASIC"
    """Basic utilities: date, time, day of week"""

    MATH = "MATH"
    """Mathematical operations: calculate, convert (future)"""

    STRING = "STRING"
    """String manipulation: spell out, format (future)"""

    CALL = "CALL"
    """Call-related: duration, metadata (future)"""

    EXTERNAL = "EXTERNAL"
    """External API wrappers without template config (future)"""


@dataclass
class CommonTool:
    """
    Represents a common tool with all its metadata.

    Attributes:
        name: Tool name the LLM will call (e.g., "get_current_datetime")
        description: Tells the LLM when to invoke this tool
        handler: Async function (args, flow_manager) -> (result_dict, None)
        category: ToolCategory this tool belongs to
        properties: OpenAI-style parameter schema (optional)
        required: Required parameter names (optional)
    """

    name: str
    description: str
    handler: Callable
    category: ToolCategory
    properties: Optional[Dict[str, Any]] = None
    required: Optional[List[str]] = None

    def to_flows_schema(self) -> FlowsFunctionSchema:
        """Convert to FlowsFunctionSchema for FlowManager."""
        return FlowsFunctionSchema(
            name=self.name,
            description=self.description,
            handler=self.handler,
            properties=self.properties or {},
            required=self.required or [],
        )


class CommonToolRegistry:
    """
    Registry for common tools available to templates.

    Tools are organized by category. Templates request categories
    via the `common_tools` configuration field.

    Example:
        # Template JSON:
        { "configurations": { "common_tools": {"BASIC": ["*"]} } }

        # Retrieve tools:
        tools = CommonToolRegistry.get_by_categories([ToolCategory.BASIC])
    """

    _tools: Dict[str, CommonTool] = {}
    _by_category: Dict[ToolCategory, List[str]] = {}

    @classmethod
    def register(cls, tool: CommonTool) -> None:
        """Register a common tool. Idempotent — re-registering overwrites silently."""
        if tool.name in cls._tools:
            old_tool = cls._tools[tool.name]
            if old_tool.category in cls._by_category:
                cls._by_category[old_tool.category] = [
                    n for n in cls._by_category[old_tool.category] if n != tool.name
                ]

        cls._tools[tool.name] = tool
        cls._by_category.setdefault(tool.category, [])
        if tool.name not in cls._by_category[tool.category]:
            cls._by_category[tool.category].append(tool.name)
        logger.info(f"Registered common tool: {tool.name} [{tool.category.value}]")

    @classmethod
    def get(cls, name: str) -> Optional[CommonTool]:
        """Get a tool by name."""
        return cls._tools.get(name)

    @classmethod
    def get_all(cls) -> List[FlowsFunctionSchema]:
        """Get all registered tools."""
        return [tool.to_flows_schema() for tool in cls._tools.values()]

    @classmethod
    def get_by_categories(
        cls, categories: List[ToolCategory]
    ) -> List[FlowsFunctionSchema]:
        """
        Get tools filtered by list of categories.

        Args:
            categories: List of ToolCategory enums to include

        Returns:
            List of FlowsFunctionSchema objects in those categories
        """
        result = []
        for category in categories:
            tool_names = cls._by_category.get(category, [])
            for name in tool_names:
                if name in cls._tools:
                    result.append(cls._tools[name].to_flows_schema())
        return result

    @classmethod
    def list_tools(cls) -> Dict[str, List[str]]:
        """List all tools grouped by category (for debugging)."""
        return {
            category.value: tool_names
            for category, tool_names in cls._by_category.items()
            if tool_names
        }

    @classmethod
    def clear(cls) -> None:
        """Clear all tools (for testing)."""
        cls._tools.clear()
        for cat in ToolCategory:
            cls._by_category[cat] = []
