"""
Utility functions for evaluator actions.

Provides JSON extraction and parsing helpers for processing evaluator comments.
"""

import json
import re
from typing import Any, Dict, Optional

from app.core.logger import logger


def extract_json_from_end(comment: str) -> Optional[Dict[str, Any]]:
    """
    Extract the LAST JSON object from comment text.

    If multiple JSON objects are found, returns the one at the end.
    Handles various formats LLMs might output:
    - Standard JSON with double quotes
    - Single quotes (JavaScript/Python style)
    - Unquoted keys (JavaScript object literal)
    - Any combination of the above

    Args:
        comment: Full comment text that may contain JSON

    Returns:
        Parsed JSON dict if found, None otherwise
    """
    if not comment:
        return None

    # Find all JSON-like objects in the comment
    # Pattern matches nested braces up to 2 levels deep
    pattern = r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}"
    matches = re.findall(pattern, comment, re.DOTALL)

    if not matches:
        return None

    # Try to parse each match from the end, return first valid one
    for match in reversed(matches):
        result = _try_parse_json_flexible(match)
        if result is not None:
            return result

    logger.warning(
        f"Found {len(matches)} JSON-like patterns but none parsed successfully"
    )
    return None


def _try_parse_json_flexible(json_str: str) -> Optional[Dict[str, Any]]:
    """
    Try multiple parsing strategies to handle various JSON formats.

    Strategies (in order of preference):
    1. Standard JSON (double quotes)
    2. Quote unquoted keys, then parse
    3. Convert single quotes to double quotes, then parse
    4. Do both transformations (handles single quotes + unquoted keys)

    Args:
        json_str: String that looks like a JSON object

    Returns:
        Parsed dict if successful, None otherwise
    """
    # Strategy 1: Standard JSON
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass

    # Strategy 2: Quote unquoted keys only
    try:
        fixed = _quote_unquoted_keys(json_str)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 3: Convert single quotes to double quotes only
    try:
        fixed = _convert_single_to_double_quotes(json_str)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass

    # Strategy 4: Both transformations (for single quotes + unquoted keys)
    try:
        fixed = _quote_unquoted_keys(json_str)
        fixed = _convert_single_to_double_quotes(fixed)
        return json.loads(fixed)
    except (json.JSONDecodeError, ValueError):
        pass

    return None


def _convert_single_to_double_quotes(s: str) -> str:
    """
    Convert single-quoted strings to double-quoted for JSON compatibility.

    Handles escaped single quotes within strings.
    Preserves existing double-quoted strings.

    Args:
        s: String that may contain single-quoted values

    Returns:
        String with single quotes converted to double quotes
    """
    result = []
    i = 0
    in_single_quote = False
    in_double_quote = False

    while i < len(s):
        char = s[i]

        if char == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
            result.append(char)
        elif char == "'" and not in_double_quote:
            if not in_single_quote:
                # Start of single-quoted string
                in_single_quote = True
                result.append('"')
            else:
                # End of single-quoted string
                in_single_quote = False
                result.append('"')
        elif char == "\\" and in_single_quote:
            # Handle escapes in single-quoted strings
            if i + 1 < len(s) and s[i + 1] == "'":
                # Escaped single quote \' -> just ' (apostrophe in double-quoted string)
                result.append("'")
                i += 1
            elif i + 1 < len(s) and s[i + 1] == '"':
                # Escaped double quote inside single-quoted string -> keep escaped
                result.append("\\")
                result.append('"')
                i += 1
            else:
                result.append(char)
        else:
            result.append(char)
        i += 1

    return "".join(result)


def _quote_unquoted_keys(s: str) -> str:
    """
    Add quotes to unquoted keys in JSON-like strings.

    Converts JavaScript-style object literals to valid JSON.
    Example: {name: "value"} -> {"name": "value"}

    Args:
        s: String that may contain unquoted keys

    Returns:
        String with quoted keys
    """
    # Match unquoted keys: identifier followed by :
    # Only matches after { or , to avoid matching values
    pattern = r"(?<=[{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:"
    return re.sub(pattern, r'"\1":', s)


def extract_field(data: Dict[str, Any], json_path: str) -> Optional[str]:
    """
    Extract field from JSON using simple path like $.field_name.

    Args:
        data: Parsed JSON dict
        json_path: Path like "$.actual_outcome"

    Returns:
        Extracted value as string, or None if not found
    """
    if not data or not json_path:
        return None

    # Simple path: $.actual_outcome -> actual_outcome
    field = json_path.replace("$.", "")
    value = data.get(field)

    # Coerce to string if value exists, return None otherwise
    if value is None:
        return None
    return str(value)
