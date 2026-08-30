"""
Field Resolver for Hooks and Global Functions

Resolves field values from different sources for hooks and global HTTP functions.
Supports field resolution with STATIC, LLM, and COMPUTED sources.

Example:
    # Static value (literal)
    config = FieldConfig(source="static", value="CONFIRMED")
    resolver.resolve_value(config)  # Returns "CONFIRMED"

    # Static value with template variable
    config = FieldConfig(source="static", value="{api_base_url}")
    resolver.resolve_value(config)  # Returns template_vars["api_base_url"]

    # LLM value
    config = FieldConfig(source="llm", value="order_id")
    resolver.resolve_value(config)  # Returns args["order_id"]

    # Computed value (dynamic at invocation time)
    config = FieldConfig(source="computed", value="utc_now_minus_hours:1")
    resolver.resolve_value(config)  # Returns "2026-03-17T09:30:00Z"
"""

import re
from typing import Any, Dict, List, Optional

from app.ai.voice.agents.breeze_buddy.handlers.transport.utils.computed_fields import (
    resolve_computed_value,
)
from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext
from app.ai.voice.agents.breeze_buddy.template.types import (
    FieldConfig,
    FieldSource,
)
from app.core.logger import logger

# Shared placeholder pattern: matches {variable_name}
PLACEHOLDER_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def replace_placeholders(template_str: str, values: Dict[str, Any]) -> str:
    """
    Replace {placeholder} patterns in a string with values from a dictionary.

    This is a shared utility used by both FieldResolver (for static values)
    and HttpRequestExecutor (for URL, body, headers, etc.).

    Args:
        template_str: String that may contain {placeholder} patterns
        values: Dictionary of placeholder_name -> value mappings

    Returns:
        String with all placeholders replaced. Unmatched placeholders are left as-is.

    Example:
        replace_placeholders("{api_url}/orders/{order_id}", {"api_url": "https://api.com", "order_id": "123"})
        # Returns: "https://api.com/orders/123"
    """
    if not isinstance(template_str, str):
        return template_str

    placeholders = PLACEHOLDER_PATTERN.findall(template_str)
    if not placeholders:
        return template_str

    result = template_str
    for placeholder in placeholders:
        if placeholder in values:
            replacement = str(values[placeholder])
            result = result.replace(f"{{{placeholder}}}", replacement)
            logger.debug(f"Replaced placeholder '{{{placeholder}}}'")
        else:
            logger.warning(
                f"Placeholder '{{{placeholder}}}' not found in values "
                f"(available: {list(values.keys())})"
            )
            # Leave placeholder as-is so it's obvious something is wrong

    return result


class FieldResolver:
    """
    Resolves field values from different sources.

    Supports simplified resolution with STATIC and LLM sources only.
    Static values can contain {variable} placeholders that get resolved
    from template_vars (payload-derived values) using the shared
    replace_placeholders() function.
    """

    def __init__(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        template_vars: Optional[Dict[str, Any]] = None,
    ):
        """
        Initialize resolver with context, LLM arguments, and template variables.

        Args:
            context: Template context (provides access to bot state)
            args: Function arguments from LLM
            template_vars: Template variables from payload (e.g., api_key, customer_name)
                          If None, will try to get from context.bot.template_vars
        """
        self.context = context
        self.args = args

        # Try to get template_vars from context if not provided
        if template_vars is None:
            if hasattr(context, "bot") and hasattr(context.bot, "template_vars"):
                self.template_vars = context.bot.template_vars or {}
            else:
                self.template_vars = {}
        else:
            self.template_vars = template_vars

    def resolve_value(
        self, config: FieldConfig, field_name: Optional[str] = None
    ) -> Optional[Any]:
        """
        Resolve a single field value based on its source.

        Args:
            config: Field configuration with source and value
            field_name: Optional field name to use as fallback for LLM source
                       when config.value is not specified. This allows
                       { "order_id": { "source": "llm" } } to automatically
                       look up args["order_id"].

        Returns:
            Resolved value or None if not found

        Raises:
            ValueError: If source type is not supported
        """
        if config.source == FieldSource.STATIC:
            return self._resolve_static(config)
        elif config.source == FieldSource.LLM:
            return self._resolve_llm(config, field_name=field_name)
        elif config.source == FieldSource.COMPUTED:
            return self._resolve_computed(config)
        elif config.source == FieldSource.TOOL_RESULT:
            return self._resolve_tool_result(config, field_name)
        else:
            raise ValueError(
                f"Unsupported field source: {config.source}. "
                f"Supported sources: STATIC, LLM, COMPUTED."
            )

    def resolve_dict(self, template: Dict[str, Any]) -> Dict[str, Any]:
        """
        Recursively resolve all values in a dictionary template.

        Args:
            template: Dictionary that may contain FieldConfig values

        Returns:
            Dictionary with all values resolved
        """
        result = {}
        for key, value in template.items():
            if isinstance(value, FieldConfig):
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
            template: List that may contain FieldConfig values

        Returns:
            List with all values resolved
        """
        result = []
        for item in template:
            if isinstance(item, FieldConfig):
                result.append(self.resolve_value(item))
            elif isinstance(item, dict):
                result.append(self.resolve_dict(item))
            elif isinstance(item, list):
                result.append(self.resolve_list(item))
            else:
                result.append(item)
        return result

    def _resolve_static(self, config: FieldConfig) -> Optional[Any]:
        """
        Resolve a static value, with optional template variable substitution.

        Static values can contain {variable_name} placeholders that get
        resolved from template_vars (derived from payload).

        Examples:
            - "CONFIRMED" → "CONFIRMED" (literal value)
            - "{api_key}" → template_vars["api_key"] (single variable)
            - "https://{api_host}/api" → "https://example.com/api" (inline replacement)

        Args:
            config: Field configuration with static value

        Returns:
            The resolved static value
        """
        value = config.value

        if value is None:
            return None

        # If not a string, return as-is (numbers, booleans, etc.)
        if not isinstance(value, str):
            logger.debug(f"Resolving STATIC non-string value: {type(value).__name__}")
            return value

        # Use shared placeholder replacement function
        return replace_placeholders(value, self.template_vars)

    def _resolve_llm(
        self, config: FieldConfig, field_name: Optional[str] = None
    ) -> Optional[Any]:
        """
        Resolve a value from LLM arguments.

        Args:
            config: Field configuration where config.value is the argument name
            field_name: Optional fallback argument name if config.value is not set.
                       This enables simplified syntax: { "order_id": { "source": "llm" } }
                       which will automatically look up args["order_id"].

        Returns:
            Value from args dictionary or None if not found
        """
        arg_name = config.value or field_name
        if not arg_name:
            logger.warning(
                "LLM source specified but no argument name provided in config.value "
                "and no field_name fallback available"
            )
            return None

        value = self.args.get(arg_name)
        if value is None:
            logger.warning(
                f"LLM argument '{arg_name}' not found in args (total args: {len(self.args)})"
            )
        else:
            logger.debug(f"Resolved LLM field '{arg_name}' successfully")

        return value

    def _resolve_tool_result(
        self, config: FieldConfig, field_name: Optional[str] = None
    ) -> Optional[Any]:
        """Resolve "<tool_name>#/<json-pointer>" against THIS turn's
        recorded tool results (the chat agent's BindingStore — the same
        store render_ui show-ops hydrate from).

        Databinding for tool ARGS: an identifier produced by one tool and
        consumed by the next hops server-side instead of being retyped by
        the LLM, which mistranscribes UUIDs often enough to break flows.
        Fail-soft: no store on this surface (voice), unknown tool, or a
        pointer miss all resolve to None with a log line.
        """
        expr = config.value
        if not isinstance(expr, str) or "#/" not in expr:
            logger.error(
                f"TOOL_RESULT value must be '<tool>#/<pointer>', got {expr!r} "
                f"(field {field_name!r})"
            )
            return None
        tool_name, pointer = expr.split("#/", 1)
        store = getattr(getattr(self.context, "bot", None), "binding_store", None)
        if store is None:
            logger.warning(
                f"TOOL_RESULT source unavailable on this surface for "
                f"field {field_name!r} ({expr!r})"
            )
            return None
        data = store.resolve(tool_name)
        for segment in [s for s in pointer.split("/") if s]:
            if isinstance(data, list):
                try:
                    data = data[int(segment)]
                except (ValueError, IndexError):
                    data = None
            elif isinstance(data, dict):
                data = data.get(segment)
            else:
                data = None
            if data is None:
                logger.warning(
                    f"TOOL_RESULT {expr!r} unresolved at segment "
                    f"{segment!r} (field {field_name!r})"
                )
                return None
        return data

    def _resolve_computed(self, config: FieldConfig) -> str:
        """
        Resolve a computed value that is dynamically generated at invocation time.

        Computed values use a function expression syntax:
            - "utc_now" -> current UTC timestamp
            - "utc_now_minus_hours:1" -> UTC timestamp minus 1 hour
            - "ist_now" -> current IST timestamp

        Args:
            config: Field configuration where config.value is the computed expression

        Returns:
            The computed string value

        Raises:
            ValueError: If the computed expression is invalid or function is unknown
        """
        expression = config.value

        if not expression:
            raise ValueError(
                "COMPUTED source requires a 'value' field with the function expression"
            )

        if not isinstance(expression, str):
            raise ValueError(
                f"COMPUTED source value must be a string expression, got {type(expression).__name__}"
            )

        logger.debug(f"Resolving COMPUTED field with expression: '{expression}'")

        try:
            result = resolve_computed_value(expression)
            logger.debug(f"Resolved COMPUTED expression '{expression}' -> '{result}'")
            return result
        except ValueError as e:
            logger.error(f"Failed to resolve COMPUTED expression '{expression}': {e}")
            raise
