"""Custom-component registry: write-time guards + session-scoped hydration.

CHAMELEON — merchant-specific components as DATA (``ui_component`` rows,
migration 057). Two halves, both pure (no DB, no agent state):

- **Registration guards** (:func:`validate_registration`): name shape and
  built-in collision, JSON-Schema well-formedness, flag rules (v1:
  data-bound only, no literal fields), and the ``render_def`` grammar lint.
  The registry API calls this before any write; rejected defs never reach
  a session.
- **Hydration** (:func:`resolve_custom_show_op`): mirrors
  ``binding.resolve_show_op`` for defs that live outside ``UI_CATALOG`` —
  same BindingStore pointer-walk (THIS turn only), single-object→list
  lift, ``items[]`` selection, caps — but validates the hydrated props
  against the def's **JSON Schema** instead of a Pydantic class. Every
  RFC-001 trust invariant survives: values come only from this turn's
  validated tool results; the model authors selectors, never values.

Isolation: nothing here is process-global. Defs arrive per-session on the
``ChatAgent`` (``custom_components`` constructor arg) — two merchants on
the same worker never see each other's definitions.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from app.ai.voice.agents.breeze_buddy.chat.ui.binding import (
    parse_bind_ref,
    resolve_json_pointer,
)
from app.ai.voice.agents.breeze_buddy.chat.ui.stream import OpResult
from app.ai.voice.agents.breeze_buddy.template.types import CustomComponentDef

# ---------------------------------------------------------------------------
# render_def grammar v1 (shared contract with the widget's declarative
# interpreter — packages/breeze-buddy-assist-widget declarative module)
# ---------------------------------------------------------------------------

# Structural node vocabulary. The interpreter drops unknown types at render
# time (never heals); the lint rejects them at write time so authors find
# out immediately.
RENDER_DEF_NODE_TYPES = frozenset(
    {
        "box",  # container; props.direction: row|col, gap, wrap, align
        "row",  # sugar: box direction=row
        "col",  # sugar: box direction=col
        "text",  # props.value (interpolatable), props.variant
        "badge",  # small emphasized text chip; props.value, props.tone
        "chip",  # alias of badge with pill styling
        "image",  # props.src (interpolatable), props.alt
        "button",  # props.label, props.variant, props.action
        "divider",
    }
)

RENDER_DEF_MAX_DEPTH = 8
RENDER_DEF_MAX_NODES = 400

_NAME_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,63}$")
_BINDING_RE = re.compile(r"^\$[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*$")
_ACTION_TYPES = frozenset({"to_assistant", "open_url", "open_detail", "intent"})


def _lint_action(action: Any, path: str, errors: List[str]) -> None:
    if not isinstance(action, dict):
        errors.append(f"{path}: action must be an object")
        return
    a_type = action.get("type")
    if a_type not in _ACTION_TYPES:
        errors.append(f"{path}: action.type must be one of {sorted(_ACTION_TYPES)}")
        return
    if a_type == "to_assistant":
        if not isinstance(action.get("msg"), str) or not action["msg"].strip():
            errors.append(f"{path}: to_assistant action needs a non-empty msg")
    if a_type == "open_url":
        url = action.get("url")
        if not isinstance(url, str) or not url:
            errors.append(f"{path}: open_url action needs a url")
        # A static URL must be https; a bound/interpolated URL resolves to
        # tool data at render time (the interpreter re-checks scheme).
        elif "{" not in url and not url.startswith("$"):
            if not url.startswith("https://"):
                errors.append(f"{path}: static open_url urls must be https")
    if a_type == "open_detail":
        # Opens the widget's detail overlay rendering ANOTHER registry
        # component with props hydrated CLIENT-side from the current
        # scope. The component name must be a static PascalCase name —
        # never a binding — so data can't choose which def paints
        # full-page; the widget additionally drops names not in the
        # session's def registry.
        component = action.get("component")
        if not isinstance(component, str) or not _NAME_RE.match(component):
            errors.append(
                f"{path}: open_detail action needs a static PascalCase component"
            )
        props = action.get("props")
        if props is not None and not isinstance(props, dict):
            errors.append(f"{path}: open_detail action props must be an object")
        title = action.get("title")
        if title is not None and not isinstance(title, str):
            errors.append(f"{path}: open_detail action title must be a string")
    if a_type == "intent":
        # Fires a template-defined DIRECT ui_intent (see
        # UiIntentsConfig.custom) and — when `component` is present —
        # opens the detail overlay armed to hydrate with that component.
        # Same static-PascalCase rule as open_detail: data must never
        # choose which def paints full-page.
        name = action.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{path}: intent action needs a non-empty name")
        component = action.get("component")
        if component is not None and (
            not isinstance(component, str) or not _NAME_RE.match(component)
        ):
            errors.append(
                f"{path}: intent action component must be a static " "PascalCase name"
            )
        payload = action.get("payload")
        if payload is not None and not isinstance(payload, dict):
            errors.append(f"{path}: intent action payload must be an object")
        title = action.get("title")
        if title is not None and not isinstance(title, str):
            errors.append(f"{path}: intent action title must be a string")


def _lint_node(
    node: Any, depth: int, path: str, counter: List[int], errors: List[str]
) -> None:
    if counter[0] > RENDER_DEF_MAX_NODES:
        return  # already reported
    if depth > RENDER_DEF_MAX_DEPTH:
        errors.append(f"{path}: exceeds max depth {RENDER_DEF_MAX_DEPTH}")
        return
    if not isinstance(node, dict):
        errors.append(f"{path}: node must be an object")
        return
    counter[0] += 1
    if counter[0] > RENDER_DEF_MAX_NODES:
        errors.append(f"render_def exceeds {RENDER_DEF_MAX_NODES} nodes")
        return
    n_type = node.get("type")
    if n_type not in RENDER_DEF_NODE_TYPES:
        errors.append(
            f"{path}: unknown node type {n_type!r} "
            f"(allowed: {sorted(RENDER_DEF_NODE_TYPES)})"
        )
    repeat = node.get("repeat")
    if repeat is not None:
        if not isinstance(repeat, dict):
            errors.append(f"{path}: repeat must be an object")
        else:
            src = repeat.get("in")
            if not (isinstance(src, str) and _BINDING_RE.match(src)):
                errors.append(
                    f"{path}: repeat.in must be a binding like '$props.journeys'"
                )
            as_name = repeat.get("as")
            if not (
                isinstance(as_name, str) and re.match(r"^[a-z][A-Za-z0-9_]*$", as_name)
            ):
                errors.append(f"{path}: repeat.as must be a lowercase identifier")
    cond = node.get("if")
    if cond is not None and not (isinstance(cond, str) and _BINDING_RE.match(cond)):
        errors.append(f"{path}: 'if' must be a binding like '$item.route'")
    cls = node.get("class")
    if cls is not None and not isinstance(cls, str):
        errors.append(f"{path}: class must be a string")
    props = node.get("props")
    if props is not None and not isinstance(props, dict):
        errors.append(f"{path}: props must be an object")
    if isinstance(props, dict) and "action" in props:
        _lint_action(props["action"], f"{path}.props.action", errors)
    children = node.get("children")
    if children is not None:
        if not isinstance(children, list):
            errors.append(f"{path}: children must be an array")
        else:
            for i, child in enumerate(children):
                _lint_node(child, depth + 1, f"{path}.children[{i}]", counter, errors)


def lint_render_def(render_def: Any) -> List[str]:
    """Lint a render_def tree. Empty list = clean.

    Grammar v1: whitelisted node types, ``repeat``/``if`` binding syntax,
    depth ≤ {depth}, ≤ {nodes} nodes, actions restricted to
    ``to_assistant``/``open_url``/``open_detail``/``intent`` (static
    open_url URLs https-only; open_detail/intent components static
    PascalCase). No
    merchant JavaScript, no arbitrary expressions — ever.
    """.format(depth=RENDER_DEF_MAX_DEPTH, nodes=RENDER_DEF_MAX_NODES)
    errors: List[str] = []
    _lint_node(render_def, 1, "$", [0], errors)
    return errors


# ---------------------------------------------------------------------------
# Registration guards (write path)
# ---------------------------------------------------------------------------


def builtin_component_names() -> set:
    """Every name a custom component may NOT take: the full built-in
    catalog including every lazy flavor group (loaded on demand so the
    check can't pass just because commerce wasn't imported yet)."""
    from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
        LAZY_GROUPS,
        UI_CATALOG,
        ensure_group_loaded,
    )

    for group in LAZY_GROUPS:
        ensure_group_loaded(group)
    return set(UI_CATALOG.keys())


def validate_registration(
    *,
    name: str,
    props_schema: Dict[str, Any],
    flags: Dict[str, Any],
    render_def: Optional[Dict[str, Any]],
) -> List[str]:
    """All write-time guard errors for one registration. Empty = accept."""
    errors: List[str] = []
    if not _NAME_RE.match(name or ""):
        errors.append(
            "name must be PascalCase (letters/digits, starting uppercase, "
            "2-64 chars)"
        )
    elif name in builtin_component_names():
        errors.append(f"name {name!r} collides with a built-in component")

    try:
        Draft202012Validator.check_schema(props_schema)
    except SchemaError as exc:
        errors.append(f"props_schema is not valid JSON Schema: {exc.message}")

    if flags.get("data_bound") is False:
        errors.append("flags.data_bound must be true in v1 (data-bound only)")
    if flags.get("literal_fields"):
        # v1: no literal fields — the engine's fail-closed gate would drop
        # them anyway (no flavor verifier exists for custom components).
        errors.append("flags.literal_fields is not supported for custom components")
    sel = flags.get("selection_field")
    if sel is not None and not isinstance(sel, str):
        errors.append("flags.selection_field must be a string prop name")
    list_props = flags.get("list_props")
    if list_props is not None and not (
        isinstance(list_props, list) and all(isinstance(p, str) for p in list_props)
    ):
        errors.append("flags.list_props must be a list of prop names")
    for cap in ("max_items_default", "max_items_limit"):
        value = flags.get(cap)
        if value is not None and not (isinstance(value, int) and value > 0):
            errors.append(f"flags.{cap} must be a positive integer")
    if flags.get("overlay_only") and render_def is None:
        # overlay_only removes the def from the model's vocabulary, so a
        # render_def is its ONLY way to ever paint — without one the row
        # is dead weight on every session surface.
        errors.append("flags.overlay_only requires a render_def")

    if render_def is not None:
        errors.extend(lint_render_def(render_def))
    return errors


# ---------------------------------------------------------------------------
# Hydration (render path) — mirrors binding.resolve_show_op
# ---------------------------------------------------------------------------


def _schema_expects_list(props_schema: Dict[str, Any], prop: str) -> bool:
    """True when the def's schema types ``prop`` as an array (drives the
    single-object→list lift, mirroring the Pydantic-annotation check)."""
    spec = (props_schema.get("properties") or {}).get(prop)
    if not isinstance(spec, dict):
        return False
    s_type = spec.get("type")
    if s_type == "array":
        return True
    if isinstance(s_type, list) and "array" in s_type:
        return True
    return any(
        isinstance(variant, dict) and variant.get("type") == "array"
        for variant in (spec.get("anyOf") or []) + (spec.get("oneOf") or [])
    )


def _apply_selection(
    hydrated: Dict[str, Any],
    def_: CustomComponentDef,
) -> None:
    """Model-directed ``items[]`` selection over the def's list props —
    id-matching only, selection order, fail-open on zero matches (the
    full tool list beats an empty render on a model-mangled id).
    Runs before capping; the selection directive itself is popped from
    the output by the caller."""
    sel_field = def_.flags.selection_field
    if not sel_field:
        return
    raw = hydrated.get(sel_field)
    if not isinstance(raw, list):
        return
    ids: List[str] = []
    for entry in raw:
        if isinstance(entry, str) and entry:
            ids.append(entry)
        elif (
            isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
        ):
            ids.append(entry["id"])
    if not ids:
        return
    for key in def_.flags.list_props:
        value = hydrated.get(key)
        if not isinstance(value, list):
            continue
        by_id: Dict[str, Any] = {}
        for entry in value:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                by_id.setdefault(entry["id"], entry)
        selected = [by_id[i] for i in ids if i in by_id]
        if selected:
            hydrated[key] = selected


def _apply_caps(hydrated: Dict[str, Any], def_: CustomComponentDef) -> None:
    """Cap bound list props: op-level ``max_items`` (or the def default),
    hard-bounded by ``max_items_limit``."""
    flags = def_.flags
    raw = hydrated.get("max_items", flags.max_items_default)
    cap = raw if isinstance(raw, int) and raw > 0 else None
    if flags.max_items_limit is not None:
        cap = min(cap, flags.max_items_limit) if cap else flags.max_items_limit
    if cap is None:
        return
    for key in flags.list_props:
        value = hydrated.get(key)
        if isinstance(value, list) and len(value) > cap:
            hydrated[key] = value[:cap]


def resolve_custom_show_op(
    op: Dict[str, Any],
    store: Any,
    def_: CustomComponentDef,
) -> OpResult:
    """Hydrate one custom-component ``show`` op against this turn's
    binding store, validated by the def's JSON Schema.

    Same shape and invariants as ``binding.resolve_show_op``; error
    strings are structural-only (paths + validator names, never values).
    """
    bind = op.get("bind") or {}
    props = op.get("props") or {}
    hydrated: Dict[str, Any] = dict(props)
    for prop, ref in bind.items():
        parsed = parse_bind_ref(ref)
        if parsed is None:
            return OpResult(error=f"bad_bind_ref:{prop}")
        payload = store.resolve(parsed.tool_name, parsed.tool_use_id)
        if payload is None:
            return OpResult(
                error=f"bind_unresolved:{parsed.tool_name}:{parsed.pointer}"
            )
        value, found = resolve_json_pointer(payload, parsed.pointer)
        if not found or value is None:
            return OpResult(
                error=f"bind_unresolved:{parsed.tool_name}:{parsed.pointer}"
            )
        if isinstance(value, dict) and (
            prop in def_.flags.list_props
            or _schema_expects_list(def_.props_schema, prop)
        ):
            value = [value]
        hydrated[prop] = value

    _apply_selection(hydrated, def_)
    _apply_caps(hydrated, def_)

    # Directives, not render props — keep them out of validation and off
    # the wire (mirrors resolve_show_op's selection_field pop).
    sel_field = def_.flags.selection_field
    if sel_field:
        hydrated.pop(sel_field, None)
    hydrated.pop("max_items", None)

    validator = Draft202012Validator(def_.props_schema)
    schema_errors = sorted(validator.iter_errors(hydrated), key=lambda e: e.json_path)
    if schema_errors:
        details = ";".join(
            f"{err.json_path}:{err.validator}" for err in schema_errors[:5]
        )
        return OpResult(error=f"bind_validation_failed:{def_.name}:{details}")

    hydrated_op: Dict[str, Any] = {
        "op": "add",
        "id": op.get("id"),
        "type": def_.name,
        "props": hydrated,
        "v": 2,
    }
    parent = op.get("parent")
    if isinstance(parent, str) and parent:
        hydrated_op["parent"] = parent
    return OpResult(op=hydrated_op)


def summarize_custom_render(
    def_: CustomComponentDef, hydrated_props: Dict[str, Any]
) -> Dict[str, Any]:
    """Function-response summary for a custom render — the model's UI
    memory. Echoes id/title-ish referents from the def's list props
    (capped) so cross-turn references ("the second option") keep working,
    mirroring the commerce summarizer's shape."""
    result: Dict[str, Any] = {"status": "ok", "rendered": def_.name}
    for key in def_.flags.list_props:
        value = hydrated_props.get(key)
        if not isinstance(value, list):
            continue
        result["count"] = len(value)
        referents: List[Dict[str, Any]] = []
        for entry in value[:8]:
            if not isinstance(entry, dict):
                continue
            ref: Dict[str, Any] = {}
            for field in ("id", "title", "name", "summary", "label"):
                if isinstance(entry.get(field), (str, int, float)):
                    ref[field] = entry[field]
            if ref:
                referents.append(ref)
        if referents:
            result[key] = referents
        break
    return result


__all__ = [
    "RENDER_DEF_MAX_DEPTH",
    "RENDER_DEF_MAX_NODES",
    "RENDER_DEF_NODE_TYPES",
    "builtin_component_names",
    "lint_render_def",
    "resolve_custom_show_op",
    "summarize_custom_render",
    "validate_registration",
]
