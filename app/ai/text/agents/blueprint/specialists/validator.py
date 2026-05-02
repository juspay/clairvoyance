"""Validator — checks couplings and required fields against the draft.

Pure function, no LLM. Runs every tick so the turn handler always sees
up-to-date violations.
"""

from __future__ import annotations

from typing import Any

from app.ai.text.agents.blueprint.schema.models import (
    ConstraintKind,
    Coupling,
    CouplingEffect,
    FieldKind,
    TemplateSchemaGraph,
)


def find_validation_issues(
    draft: dict[str, Any], schema: TemplateSchemaGraph
) -> list[str]:
    """Return the sorted, de-duplicated list of human-readable issues."""
    return sorted(set(_find_issues(draft, schema)))


def _find_issues(draft: dict[str, Any], schema: TemplateSchemaGraph) -> list[str]:
    issues: list[str] = []
    issues.extend(_coupling_issues(draft, schema.couplings))
    issues.extend(_required_top_level_issues(draft, schema))
    return issues


def _coupling_issues(draft: dict[str, Any], couplings: list[Coupling]) -> list[str]:
    out: list[str] = []
    for c in couplings:
        if not c.fires(draft):
            continue
        for eff in c.effects:
            if not _effect_satisfied(eff, draft):
                out.append(_format_coupling_issue(c, eff))
    return out


def _effect_satisfied(eff: CouplingEffect, draft: dict[str, Any]) -> bool:
    value = _resolve(draft, eff.path)
    if eff.kind == ConstraintKind.REQUIRED:
        return value is not None
    if eff.kind == ConstraintKind.EQUALS:
        return value == eff.value
    if eff.kind == ConstraintKind.FORBIDDEN:
        return value is None
    return True


def _format_coupling_issue(c: Coupling, eff: CouplingEffect) -> str:
    trigger_bits = ", ".join(f"{p}={v!r}" for p, v in c.trigger.items())
    base = f"[{c.name}] {trigger_bits} → "
    if eff.kind == ConstraintKind.REQUIRED:
        return f"{base}{eff.path} is required. {eff.reason or c.reason}"
    if eff.kind == ConstraintKind.EQUALS:
        return f"{base}{eff.path} must equal {eff.value!r}. {eff.reason or c.reason}"
    if eff.kind == ConstraintKind.FORBIDDEN:
        return f"{base}{eff.path} must be unset. {eff.reason or c.reason}"
    return base + "unsatisfied"


def _required_top_level_issues(
    draft: dict[str, Any], schema: TemplateSchemaGraph
) -> list[str]:
    out: list[str] = []
    for field in schema.fields:
        if "." in field.path:
            continue
        if not field.required or field.deprecated:
            continue
        if field.kind == FieldKind.OPAQUE:
            continue
        if _resolve(draft, field.path) in (None, "", [], {}):
            out.append(f"[required] {field.path} is not set.")
    return out


def _resolve(data: dict[str, Any], path: str) -> Any:
    cur: Any = data
    for seg in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(seg)
        if cur is None:
            return None
    return cur


__all__ = ["find_validation_issues"]
