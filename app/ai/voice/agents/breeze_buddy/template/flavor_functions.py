"""Function entries a flavor adds from its own switches.

A flavor registers a provider on import; the builder appends the entries of
each enabled group next to the knowledge-base tool. As for that tool, a
template that already declares one of a provider's names keeps its own.
"""

from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from app.core.logger import logger

# configurations -> function entries (``[]`` when the switch is off).
FlavorFunctionsFn = Callable[[Any], List[Dict[str, Any]]]

_PROVIDERS: List[Tuple[str, FlavorFunctionsFn]] = []


def register_flavor_functions(group: str, fn: FlavorFunctionsFn) -> None:
    """Add a provider under ``group`` (idempotent for the same function)."""
    if all(existing is not fn for _, existing in _PROVIDERS):
        _PROVIDERS.append((group, fn))


def _configurations(bot_instance: Any) -> Any:
    configurations = getattr(bot_instance, "configurations", None)
    if configurations is None:
        template = getattr(bot_instance, "template", None)
        configurations = getattr(template, "configurations", None)
    return configurations


def synthesize_flavor_functions(
    bot_instance: Any,
    log: Optional[Callable[[str], None]] = None,
) -> List[List[Dict[str, Any]]]:
    """One list per provider of an enabled group, in registration order."""
    configurations = _configurations(bot_instance)
    catalog = getattr(configurations, "ui_catalog", None)
    groups = list(getattr(catalog, "enabled_groups", None) or [])
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
            entries = fn(configurations)
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
    reserved: Iterable[str] = (),
) -> List[Dict[str, Any]]:
    """Append each provider's entries as one set, unless the template already
    declares any of their names (in ``declared`` or, in flow mode, on a node:
    ``reserved``). The LLM must never get duplicate tool names."""
    taken = set(reserved) | {
        entry.get("name") for entry in declared if isinstance(entry, dict)
    }
    result = list(declared)
    for entries in contributed:
        names = {entry.get("name") for entry in entries}
        if names & taken:
            logger.warning(
                f"flavor functions {sorted(str(n) for n in names & taken)} are already declared "
                "by the template; the template's own are kept"
            )
            continue
        result.extend(entries)
        taken |= names
    return result


__all__ = [
    "FlavorFunctionsFn",
    "append_flavor_functions",
    "register_flavor_functions",
    "synthesize_flavor_functions",
]
