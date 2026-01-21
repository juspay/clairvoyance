"""
Log Context Module - Provides scoped logging context using contextvars.

This module enables automatic injection of arbitrary key-value context fields
into all log messages within the current async scope, making it easy to trace
and filter logs in a multi-task async environment.

Usage:
    from app.core.logger.context import set_log_context, clear_log_context, update_log_context

    # At the start of a scope
    set_log_context(lead_id="abc123", call_sid="xyz")

    # When more context becomes available
    update_log_context(conversation_id="Customer-Shop-2026-01-12_08-17-39")

    # All subsequent logs will automatically include the context fields
    logger.info("Processing order")  # Will include lead_id, call_sid in JSON output

    # At the end of the scope
    clear_log_context()

Note:
    ContextVars work correctly with async/await and asyncio.create_task().
    However, they do NOT propagate to ThreadPoolExecutor threads automatically.
    This means ~90% of logs (all async code) will have context, but some low-level
    Pipecat audio processing logs from threads won't have these fields.
"""

from contextvars import ContextVar
from typing import Any, Dict

# Single context variable holding all scoped key-value pairs.
# NOTE: We intentionally avoid a mutable default like default={}.
# Using a sentinel ensures each async context starts clean.
_log_context_var: ContextVar[Dict[str, Any]] = ContextVar("log_context")


def set_log_context(**kwargs: Any) -> None:
    """
    Set the log context for the current async task/coroutine.

    Replaces any existing context with the provided key-value pairs.
    This context will be automatically included in all log messages
    within the same async context.

    Args:
        **kwargs: Arbitrary key-value pairs to include in log context.
    """
    _log_context_var.set(dict(kwargs))


def update_log_context(**kwargs: Any) -> None:
    """
    Update the log context with additional fields.

    Merges new fields into the existing context. Use this to add fields
    as they become available during the flow.

    Args:
        **kwargs: Key-value pairs to merge into the existing context.
    """
    try:
        ctx = _log_context_var.get().copy()
    except LookupError:
        ctx = {}
    ctx.update(kwargs)
    _log_context_var.set(ctx)


def clear_log_context() -> None:
    """
    Clear all log context variables.

    Should be called at the end of a scope to prevent context leakage.
    """
    _log_context_var.set({})


def get_log_context() -> Dict[str, Any]:
    """
    Get all log context variables as a dictionary.

    Returns:
        dict: Dictionary containing all set context variables.
    """
    try:
        return _log_context_var.get().copy()
    except LookupError:
        return {}
