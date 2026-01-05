"""
Field Resolver for Template System

Resolves field values from different sources for hooks and HTTP requests.
Supports simplified field resolution with STATIC and LLM sources only.

Example:
    # Static value
    config = HookFieldConfig(source="static", value="CONFIRMED")
    resolver.resolve_value(config)  # Returns "CONFIRMED"

    # LLM value
    config = HookFieldConfig(source="llm", value="order_id")
    resolver.resolve_value(config)  # Returns args["order_id"]
"""

from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    HookFieldConfig,
    HookFieldConfigSource,
)
from app.core.logger import logger


class FieldResolver:
    """
    Resolves field values from different sources.

    Supports simplified resolution with STATIC and LLM sources only.
    """

    def __init__(self, context: TemplateContext, args: Dict[str, Any]):
        """
        Initialize resolver with context and LLM arguments.

        Args:
            context: Template context (not used in simplified version but kept for consistency)
            args: Function arguments from LLM
        """
        self.context = context
        self.args = args

    def resolve_value(self, config: HookFieldConfig) -> Optional[Any]:
        """
        Resolve a single field value based on its source.

        Args:
            config: Field configuration with source and value

        Returns:
            Resolved value or None if not found

        Raises:
            ValueError: If source type is not supported
        """
        if config.source == HookFieldConfigSource.STATIC:
            return self._resolve_static(config)
        elif config.source == HookFieldConfigSource.LLM:
            return self._resolve_llm(config)
        else:
            raise ValueError(
                f"Unsupported field source: {config.source}. "
                f"Only STATIC and LLM sources are supported."
            )

    def resolve_dict(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively resolve all values in a dictionary template.

        Args:
            template: Dictionary that may contain HookFieldConfig values

        Returns:
            Dictionary with all values resolved
        """
        result = {}
        for key, value in template.items():
            if isinstance(value, HookFieldConfig):
                result[key] = self.resolve_value(value)
            elif isinstance(value, dict):
                result[key] = self.resolve_dict(value)
            elif isinstance(value, list):
                result[key] = self.resolve_list(value)
            else:
                result[key] = value
        return result

    def resolve_list(self, template: List[Any]) -> List[Any]:
        """
        Recursively resolve all values in a list template.

        Args:
            template: List that may contain HookFieldConfig values

        Returns:
            List with all values resolved
        """
        result = []
        for item in template:
            if isinstance(item, HookFieldConfig):
                result.append(self.resolve_value(item))
            elif isinstance(item, dict):
                result.append(self.resolve_dict(item))
            elif isinstance(item, list):
                result.append(self.resolve_list(item))
            else:
                result.append(item)
        return result

    def _resolve_static(self, config: HookFieldConfig) -> Optional[Any]:
        """
        Resolve a static value.

        Args:
            config: Field configuration with static value

        Returns:
            The static value from config.value
        """
        logger.debug(f"Resolving STATIC value: {config.value}")
        return config.value

    def _resolve_llm(self, config: HookFieldConfig) -> Optional[Any]:
        """
        Resolve a value from LLM arguments.

        Args:
            config: Field configuration where config.value is the argument name

        Returns:
            Value from args dictionary or None if not found
        """
        arg_name = config.value
        if not arg_name:
            logger.warning(
                "LLM source specified but no argument name provided in config.value"
            )
            return None

        value = self.args.get(arg_name)
        if value is None:
            logger.warning(
                f"LLM argument '{arg_name}' not found in args. Available args: {list(self.args.keys())}"
            )
        else:
            logger.debug(f"Resolving LLM value for '{arg_name}': {value}")

        return value
