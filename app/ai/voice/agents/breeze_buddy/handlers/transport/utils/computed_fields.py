"""
Computed Field Functions for Dynamic Value Resolution

Provides a registry of computed functions that can be used in field resolution
with source="computed". These functions are pure (no side effects) and return
string values at invocation time.

Usage in template JSON:
    "expected_fields": {
        "from_date": { "source": "computed", "value": "utc_now_minus_hours:1" },
        "to_date": { "source": "computed", "value": "utc_now" }
    }

Supported functions:
    - utc_now: Current UTC time as ISO 8601 string
    - utc_now_minus_hours:N: UTC time minus N hours (N must be positive integer)
    - ist_now: Current IST time as ISO 8601 string with timezone
    - utc_today_start: Start of today in UTC (00:00:00Z)
    - utc_today_end: End of today in UTC (23:59:59.999Z)
"""

import inspect
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict

from app.core.logger import logger

IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def utc_now() -> str:
    """Return current UTC time as ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_minus_hours(hours: int) -> str:
    """Return UTC time minus specified hours as ISO 8601 string."""
    if hours < 0:
        raise ValueError(f"hours must be non-negative, got {hours}")
    result = datetime.now(timezone.utc) - timedelta(hours=hours)
    return result.strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_now() -> str:
    """Return current IST time as ISO 8601 string with timezone offset."""
    return datetime.now(IST_OFFSET).strftime("%Y-%m-%dT%H:%M:%S%z")


def utc_today_start() -> str:
    """Return start of today in UTC (00:00:00Z) as ISO 8601 string."""
    today = datetime.now(timezone.utc).date()
    return datetime.combine(today, datetime.min.time(), tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def utc_today_end() -> str:
    """Return end of today in UTC (23:59:59.999Z) as ISO 8601 string."""
    today = datetime.now(timezone.utc).date()
    end_time = datetime.combine(today, datetime.max.time(), tzinfo=timezone.utc)
    return end_time.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


COMPUTED_FUNCTIONS: Dict[str, Callable] = {
    "utc_now": utc_now,
    "utc_now_minus_hours": utc_now_minus_hours,
    "ist_now": ist_now,
    "utc_today_start": utc_today_start,
    "utc_today_end": utc_today_end,
}


def resolve_computed_value(expression: str) -> str:
    """
    Resolve a computed field expression to its value.

    Expression format: "function_name" or "function_name:arg"

    Args:
        expression: The computed expression to resolve

    Returns:
        The computed string value

    Raises:
        ValueError: If the function is unknown or arguments are invalid

    Examples:
        resolve_computed_value("utc_now") -> "2026-03-17T10:30:00Z"
        resolve_computed_value("utc_now_minus_hours:1") -> "2026-03-17T09:30:00Z"
    """
    if not expression:
        raise ValueError("Computed expression cannot be empty")

    if ":" in expression:
        func_name, arg_str = expression.split(":", 1)
        func_name = func_name.strip()
        arg_str = arg_str.strip()
    else:
        func_name = expression.strip()
        arg_str = None

    if func_name not in COMPUTED_FUNCTIONS:
        available = list(COMPUTED_FUNCTIONS.keys())
        raise ValueError(
            f"Unknown computed function: '{func_name}'. "
            f"Available functions: {available}"
        )

    func = COMPUTED_FUNCTIONS[func_name]

    # Arity check: verify argument matches function signature
    sig = inspect.signature(func)
    params = [
        p for p in sig.parameters.values() if p.default is inspect.Parameter.empty
    ]
    expects_arg = len(params) > 0

    if arg_str is not None and not expects_arg:
        raise ValueError(
            f"Function '{func_name}' takes no arguments but got '{arg_str}'"
        )
    if arg_str is None and expects_arg:
        raise ValueError(
            f"Function '{func_name}' requires an argument but none provided"
        )

    try:
        if arg_str is not None:
            if func_name == "utc_now_minus_hours":
                try:
                    hours = int(arg_str)
                    result = func(hours)
                except ValueError as e:
                    raise ValueError(
                        f"Invalid argument for '{func_name}': expected integer, got '{arg_str}'"
                    ) from e
            else:
                result = func(arg_str)
        else:
            result = func()

        logger.debug(f"Resolved computed expression '{expression}' -> '{result}'")
        return result

    except Exception as e:
        logger.error(f"Error resolving computed expression '{expression}': {e}")
        raise
