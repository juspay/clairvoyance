"""Functions a flavor contributes to a session from its own switches.

A flavor registers a provider on import (``register_flavor_functions``); the
builder asks every provider of the template's enabled groups for function
entries (the same dicts ``flow.functions`` holds) and appends them next to
the knowledge-base tool. Core never learns which functions exist or which
switch turns them on: the provider reads its own flavor block.

A template that declares any of a provider's names keeps its own, whole.
"""

from typing import Any, Callable, Dict, List, Optional, Tuple

from app.core.logger import logger

FlavorFunctionsFn = Callable[[Any], List[Dict[str, Any]]]

_PROVIDERS: List[Tuple[str, FlavorFunctionsFn]] = []


def register_flavor_functions(group: str, fn: FlavorFunctionsFn) -> None:
    """Add a provider under ``group`` (idempotent for the same function)."""
    if all(existing is not fn for _, existing in _PROVIDERS):
        _PROVIDERS.append((group, fn))


def _enabled_groups(bot_instance: Any) -> List[str]:
    configurations = getattr(bot_instance, "configurations", None)
    if configurations is None:
        template = getattr(bot_instance, "template", None)
        configurations = getattr(template, "configurations", None)
    catalog = getattr(configurations, "ui_catalog", None)
    return list(getattr(catalog, "enabled_groups", None) or [])


def synthesize_flavor_functions(
    bot_instance: Any,
    log: Optional[Callable[[str], None]] = None,
) -> List[List[Dict[str, Any]]]:
    """One list per provider of an enabled group, in registration order."""
    groups = _enabled_groups(bot_instance)
    if not groups:
        return []
    from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
        ensure_group_loaded,
    )

    for group in groups:
        ensure_group_loaded(group)
    out: List[List[Dict[str, Any]]] = []
    for group, fn in list(_PROVIDERS):
        if group not in groups:
            continue
        try:
            entries = fn(bot_instance)
        except Exception:  # noqa: BLE001 — a flavor is never load-bearing
            logger.exception(f"flavor functions provider for {group!r} raised; skipped")
            continue
        if entries:
            (log or logger.info)(
                f"flavor {group!r} contributed functions: "
                f"{[e.get('name') for e in entries]}"
            )
            out.append(entries)
    return out


def append_flavor_functions(
    declared: List[Dict[str, Any]],
    contributed: List[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Append each provider's entries unless the template declares one of
    their names — explicit declarations win, and the LLM must never receive
    duplicate tool names."""
    result = list(declared)
    names = {entry.get("name") for entry in declared if isinstance(entry, dict)}
    for entries in contributed:
        contributed_names = {entry.get("name") for entry in entries}
        if names & contributed_names:
            logger.info(
                f"flavor functions {sorted(map(str, contributed_names))} skipped: the "
                "template declares its own"
            )
            continue
        result.extend(entries)
        names |= contributed_names
    return result


__all__ = [
    "FlavorFunctionsFn",
    "append_flavor_functions",
    "register_flavor_functions",
    "synthesize_flavor_functions",
]
