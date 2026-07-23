"""Deprecation utilities with structured logging.

Provides a function decorator and a Pydantic model helper that log
deprecation warnings with full log context (lead_id, call_sid, etc.)
automatically via the loguru patcher.

Usage — function/method::

    from app.core.deprecation import deprecated

    @deprecated("Use create_stt_from_config() instead")
    async def old_function():
        ...

Usage — Pydantic model fields::

    from app.core.deprecation import log_deprecated_fields

    class MyModel(BaseModel):
        old_field: Optional[str] = None  # deprecated

        @model_validator(mode="after")
        def _warn_deprecated(self):
            log_deprecated_fields(self, {
                "old_field": "new_config.field",
            })
            return self
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel

from app.core.logger import logger

# Track which deprecations have been logged per-process to avoid log spam
# for module-level singletons. Per-call deprecations (Pydantic models) are
# intentionally NOT deduped — each occurrence is useful for tracking migration.
_warned_functions: set[str] = set()


def deprecated(
    reason: str,
    *,
    replacement: str | None = None,
) -> Callable:
    """Decorator that logs a deprecation warning on first use per process.

    Works with both sync and async functions/methods. The warning includes
    full log context (lead_id, call_sid, etc.) automatically.

    Args:
        reason: Why this is deprecated and what happened.
        replacement: What to use instead (shown in log message).

    Example::

        @deprecated("Legacy env-var routing", replacement="create_stt_from_config()")
        async def get_stt_service(language_hints=None):
            ...
    """

    def decorator(func: Callable) -> Callable:
        qualname = func.__qualname__
        module = func.__module__

        def _log_once():
            key = f"{module}.{qualname}"
            if key in _warned_functions:
                return
            _warned_functions.add(key)

            parts = [f"DEPRECATED: {qualname}() — {reason}"]
            if replacement:
                parts.append(f"Use {replacement} instead.")
            logger.warning(" ".join(parts))

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                _log_once()
                return await func(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            _log_once()
            return func(*args, **kwargs)

        return sync_wrapper

    return decorator


def _sanitize(value: Any) -> str:
    """Strip control characters so metadata can't corrupt log records."""
    return "".join(ch for ch in str(value) if ch.isprintable())


def format_template_ref(
    template_id: Optional[str] = None,
    template_name: Optional[str] = None,
) -> str:
    """Build a ``" (template_id=..., template_name='...')"`` log suffix."""
    if not template_id:
        return ""
    ref = f" (template_id={_sanitize(template_id)}"
    if template_name:
        ref += f", template_name='{_sanitize(template_name)}'"
    return ref + ")"


def log_deprecated_fields(
    instance: BaseModel,
    field_map: Dict[str, str],
    context: Optional[Dict[str, Any]] = None,
) -> None:
    """Log warnings for deprecated Pydantic fields that were explicitly set.

    Only fires when a field was present in the input data (not for default
    values). Each occurrence is logged — useful in production to track which
    templates still use legacy fields.

    The log automatically includes full context (lead_id, call_sid, etc.)
    via the loguru patcher.

    Args:
        instance: The Pydantic model instance to check.
        field_map: Mapping of ``{deprecated_field: replacement_path}``.
        context: Optional identity info (e.g. Pydantic validation context
            with ``template_id`` / ``template_name``) appended to the message.

    Example::

        class ConfigurationModel(BaseModel):
            stt_language: Optional[str] = None

            @model_validator(mode="after")
            def _warn_deprecated(self):
                log_deprecated_fields(self, {
                    "stt_language": "stt_configuration.language",
                    "soniox_context": "stt_configuration.soniox.context",
                })
                return self
    """
    ctx = context if isinstance(context, dict) else {}
    ref = format_template_ref(ctx.get("template_id"), ctx.get("template_name"))

    for old_field, new_path in field_map.items():
        if old_field in instance.model_fields_set:
            value = getattr(instance, old_field, None)
            if value is None or value is False:
                continue
            # Log metadata only — raw values may contain sensitive data
            # (IDs, context JSON, etc.)
            type_name = type(value).__name__
            try:
                length = len(str(value))
            except Exception:
                length = -1
            logger.warning(
                "[Deprecated] field '{}' is set (type: {}, length: {}){}. Use '{}' instead.",
                old_field,
                type_name,
                length,
                ref,
                new_path,
            )
