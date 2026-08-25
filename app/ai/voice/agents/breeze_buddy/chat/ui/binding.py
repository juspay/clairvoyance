"""Server-side data binding for catalog-v2 ``show`` ops (RFC-001).

The LLM emits a ``show`` op naming a data-bound component plus *bindings*
into this turn's tool results — it never re-types tool data. This module
owns the resolution side:

- :class:`BindingStore` — per-turn record of ``(tool_name, tool_use_id) →
  validated post-pipeline result`` (populated in ``ChatAgent._cycle_loop``).
- :func:`parse_bind_ref` — the ``$tool:<name>[@<tool_use_id>]#<pointer>``
  grammar (JSON Pointer per RFC 6901).
- :func:`resolve_show_op` — pointer-walk the store, hydrate + cap props,
  validate against the component schema, and return the ordinary
  ``{"op":"add", …, "v":2}`` op the widget renders.

Invariants (RFC-001 §8): hydration only ever reads THIS turn's validated
tool results — UI freshness is enforced by code, a stale bind drops with
``bind_unresolved:*`` telemetry; an invalid ``show`` degrades to nothing
plus telemetry, never a half-hydrated render.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    get_args,
    get_origin,
)

from pydantic import ValidationError

from app.ai.voice.agents.breeze_buddy.chat.ui.stream import OpResult

# Envelope helpers shared with the state-reducer engine — same semantics on
# purpose (a result the reducers would skip is a result binds must not see).
# Private-name import is deliberate: re-implementing them here would drift.
from app.ai.voice.agents.breeze_buddy.template.session_state import (
    _is_tool_success,
    _unwrap_tool_payload,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    UI_CATALOG,
    is_data_bound,
    validate_props,
)

# ---------------------------------------------------------------------------
# Bind-ref grammar — $tool:<tool_name>[@<tool_use_id>]#<json-pointer>
# ---------------------------------------------------------------------------

# Tool names / tool_use_ids are identifier-ish tokens; the pointer is either
# empty (whole payload) or a RFC 6901 pointer starting with "/".
_BIND_REF_RE = re.compile(
    r"^\$tool:(?P<tool>[A-Za-z0-9_\-.]+)"
    r"(?:@(?P<tool_use_id>[A-Za-z0-9_\-.]+))?"
    r"#(?P<pointer>(?:/[^/]*)*)$"
)


@dataclass(frozen=True)
class BindRef:
    """One parsed binding reference."""

    tool_name: str
    tool_use_id: Optional[str]
    pointer: str  # "" (whole payload) or RFC 6901 pointer ("/products", …)


def parse_bind_ref(ref: str) -> Optional[BindRef]:
    """Parse one ``$tool:…#…`` reference; ``None`` when the grammar doesn't
    match (caller surfaces ``bad_bind_ref`` telemetry)."""
    if not isinstance(ref, str):
        return None
    m = _BIND_REF_RE.match(ref)
    if m is None:
        return None
    return BindRef(
        tool_name=m.group("tool"),
        tool_use_id=m.group("tool_use_id"),
        pointer=m.group("pointer") or "",
    )


def resolve_json_pointer(doc: Any, pointer: str) -> Tuple[Any, bool]:
    """Walk an RFC 6901 JSON Pointer. Returns ``(value, found)``.

    ``found=False`` for any miss — missing key, out-of-range / non-integer
    list index, or descending into a scalar. ``""`` returns the whole doc.
    """
    if pointer == "":
        return doc, True
    cur = doc
    for token in pointer.split("/")[1:]:
        token = token.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, dict):
            if token not in cur:
                return None, False
            cur = cur[token]
        elif isinstance(cur, list):
            if not token.isdigit() or (len(token) > 1 and token[0] == "0"):
                return None, False
            idx = int(token)
            if idx >= len(cur):
                return None, False
            cur = cur[idx]
        else:
            return None, False
    return cur, True


# ---------------------------------------------------------------------------
# Per-turn binding store
# ---------------------------------------------------------------------------


@dataclass
class BindingStore:
    """This turn's successful, post-pipeline tool results, bind-addressable.

    ``record`` is called after ``apply_result_pipeline`` ran (inside the
    dispatch path) — the stored payload is exactly what the LLM saw, with
    the FlowResult envelope unwrapped. Error envelopes are skipped so a
    bind can never hydrate from a failed call. One store per turn: never
    reused across turns (the UI-freshness rule lives here, not in prompt).
    """

    # Insertion-ordered: "latest result of <tool>" == last matching entry.
    _entries: List[Tuple[str, Optional[str], Any]] = field(default_factory=list)

    def record(self, tool_name: str, tool_use_id: Optional[str], result: Any) -> bool:
        """Record one tool result. Returns True when stored (success
        envelope), False when skipped (error envelope)."""
        if not _is_tool_success(result):
            return False
        payload = _unwrap_tool_payload(result)
        self._entries.append((tool_name, tool_use_id, payload))
        return True

    def resolve(self, tool_name: str, tool_use_id: Optional[str] = None) -> Any:
        """Latest payload for ``tool_name`` (optionally pinned to one
        ``tool_use_id``). ``None`` when the tool didn't run this turn."""
        for name, use_id, payload in reversed(self._entries):
            if name != tool_name:
                continue
            if tool_use_id is not None and use_id != tool_use_id:
                continue
            return payload
        return None

    def payloads(self) -> List[Any]:
        """All recorded payloads this turn — for trust scans (e.g. the
        LinkButton URL check: a link is renderable only if a tool result
        actually contained it)."""
        return [payload for _, _, payload in self._entries]


# ---------------------------------------------------------------------------
# show-op resolution → hydrated add op
# ---------------------------------------------------------------------------


# Selector-entry transforms — flavor-registered rewrites keyed by the extra
# selector key that triggers them (e.g. commerce's ``feature_variant``,
# which re-derives a product entry's hero from its own variant record).
# The transform receives ``(entry, value)`` and returns the (possibly
# rewritten) entry; entries stay tool-sourced by construction because a
# transform only recombines fields the entry itself carries. Registered by
# flavor packages at lazy load (same lifecycle as the other flavor
# registries); the engine knows only the mechanism.
SelectorTransformFn = Callable[[Dict[str, Any], str], Dict[str, Any]]

# group → selector key → transform. Group-keyed for the same reason the
# other flavor registries are (see ``chat/flavors.py``): these keys splice
# into the LLM-facing ``render_ui`` schema, so an ungated registry would
# advertise a commerce selector to every merchant sharing the process.
_SELECTOR_TRANSFORMS: Dict[str, Dict[str, SelectorTransformFn]] = {}


def register_selector_transform(
    group: str, key: str, transform: SelectorTransformFn
) -> None:
    """Register a flavor's per-entry selector transform for ``key``.

    Idempotent on re-import (same-key overwrite)."""
    _SELECTOR_TRANSFORMS.setdefault(group, {})[key] = transform


def selector_extension_keys(
    flavor_groups: Optional[Sequence[str]] = None,
) -> List[str]:
    """The extra selector keys (beyond ``id``) registered by the enabled
    flavor groups — the render_ui schema and arg parsing accept exactly
    these. No groups, no extensions."""
    keys: List[str] = []
    for group in flavor_groups or ():
        for key in _SELECTOR_TRANSFORMS.get(group, {}):
            if key not in keys:
                keys.append(key)
    return keys


def _select_list_props(
    hydrated: Dict[str, Any],
    schema: Any,
    bound_keys: Set[str],
    flavor_groups: Optional[Sequence[str]] = None,
) -> None:
    """Model-directed selection over bound lists (runs BEFORE capping).

    When the component schema declares a ``selection_field`` (e.g.
    ``items`` = ``[{id, …}]``) and the op carries one, every bound list
    prop is filtered to the entries whose ``id`` appears in the selection
    — in selection ORDER. This is the deterministic answer to fuzzy
    result ranking: the user asks for one thing, the tool page contains
    ten, and the match may sit at ANY rank — the model names which id(s)
    to show while every rendered VALUE stays tool-sourced (an id absent
    from the tool payload selects nothing, so the model cannot inject
    items). Extra selector keys apply their flavor-registered transform
    per entry (see :func:`register_selector_transform`). No id matching
    at all → the filter is ignored entirely (fail-open: the full tool
    list beats an empty render on a model-mangled id).
    """
    sel_field = getattr(schema, "selection_field", None)
    if not sel_field or sel_field not in schema.model_fields:
        return
    raw = hydrated.get(sel_field)
    if not isinstance(raw, list):
        return
    selectors: List[Dict[str, Any]] = []
    for entry in raw:
        if isinstance(entry, str) and entry:
            selectors.append({"id": entry})
        elif (
            isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
        ):
            selectors.append(entry)
    if not selectors:
        return
    transforms: Dict[str, SelectorTransformFn] = {}
    for group in flavor_groups or ():
        for sel_key, transform in _SELECTOR_TRANSFORMS.get(group, {}).items():
            transforms.setdefault(sel_key, transform)
    for key in bound_keys:
        value = hydrated.get(key)
        if not isinstance(value, list):
            continue
        by_id: Dict[str, Any] = {}
        for entry in value:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                by_id.setdefault(entry["id"], entry)
        selected: List[Any] = []
        for sel in selectors:
            entry = by_id.get(sel["id"])
            if entry is None:
                continue
            for sel_key, transform in transforms.items():
                sel_value = sel.get(sel_key)
                if isinstance(sel_value, str) and sel_value:
                    entry = transform(entry, sel_value)
            selected.append(entry)
        if selected:
            hydrated[key] = selected


def _expects_list(annotation: Any) -> bool:
    """True when a pydantic field annotation is (or can be) a list —
    unwraps Optional/Union so ``Optional[List[ProductP]]`` counts."""
    origin = get_origin(annotation)
    if origin is list:
        return True
    if origin is Union:
        return any(_expects_list(arg) for arg in get_args(annotation))
    return False


def _cap_list_props(
    hydrated: Dict[str, Any], schema: Any, bound_keys: Set[str]
) -> None:
    """Cap bound list props in place.

    Two ceilings apply: an explicit ``max_items`` (the prop value when the
    op carries one, else the schema default — ProductGrid's ≤8-visible
    guidance) and the field's own ``max_length`` metadata (so an oversize
    tool array is trimmed instead of failing validation — the tool data is
    valid, the render is just capped).
    """
    max_items: Optional[int] = None
    if "max_items" in schema.model_fields:
        raw = hydrated.get("max_items", schema.model_fields["max_items"].default)
        if isinstance(raw, int) and raw > 0:
            max_items = raw
    for key in bound_keys:
        value = hydrated.get(key)
        if not isinstance(value, list):
            continue
        fld = schema.model_fields.get(key)
        max_length: Optional[int] = None
        if fld is not None:
            for meta in fld.metadata:
                candidate = getattr(meta, "max_length", None)
                if isinstance(candidate, int):
                    max_length = candidate
        caps = [c for c in (max_items, max_length) if c is not None]
        if caps and len(value) > min(caps):
            hydrated[key] = value[: min(caps)]


def resolve_show_op(
    op: Dict[str, Any],
    store: BindingStore,
    allowlist: Optional[Set[str]] = None,
    flavor_groups: Optional[Sequence[str]] = None,
) -> OpResult:
    """Hydrate one parsed ``show`` op against this turn's binding store.

    ``op`` is the *validated-but-unhydrated* dict ``parse_op_line`` returned
    (component known + allowlisted + data_bound, bind refs grammar-checked,
    no bind/props collision). This function owns the data side:

    1. Pointer-walk every bind ref into the store — a tool that didn't run
       this turn or a pointer that resolves to nothing drops the whole op
       with ``bind_unresolved:<tool>:<pointer>`` (never a stale/partial render).
    2. Merge literal props, cap bound list lengths (``max_items`` + field
       ``max_length``), validate the hydrated props via the component schema.
    3. Return the ordinary hydrated add op the widget already knows how to
       render, marked ``v:2`` for telemetry + client gating.
    """
    component = op.get("component")
    if not isinstance(component, str) or not is_data_bound(component):
        # Defensive — parse_op_line already gates this.
        return OpResult(error=f"show_unknown_component:{component!r}")
    if allowlist is not None and component not in allowlist:
        return OpResult(error=f"show_component_disabled:{component}")
    schema = UI_CATALOG[component]

    # Literal fields (model-authored, trust-gated — e.g. OrderStatus's
    # transcribed ETA) must NEVER be bind targets: a bind pointer-walks
    # the RAW tool payload, which would (a) skip the flavor's anchoring
    # verifier entirely and (b) reach payload fields the projection
    # deliberately never renders. This is the one choke point both the
    # render_ui function path and the text-channel show path traverse, so
    # the gate cannot be bypassed on either. Verified literal values
    # arrive via props (execute_render_ui merges them post-verification).
    declared_literals = tuple(getattr(schema, "literal_fields", ()) or ())
    literal_binds = sorted(set(op.get("bind") or {}) & set(declared_literals))
    if literal_binds:
        return OpResult(error=f"literal_field_not_bindable:{','.join(literal_binds)}")

    bind = op.get("bind") or {}
    props = op.get("props") or {}
    hydrated: Dict[str, Any] = dict(props)
    bound_keys: Set[str] = set()
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
        if isinstance(value, dict):
            fld = schema.model_fields.get(prop)
            if fld is not None and _expects_list(fld.annotation):
                # A single tool-result object feeding a list-shaped prop
                # hydrates as a one-element list — e.g. get_product's
                # ``product`` → ProductGrid.products. With layout derived
                # from the hydrated count, a grid of one IS the card, so
                # single-object results need no separate component.
                value = [value]
        hydrated[prop] = value
        bound_keys.add(prop)

    _select_list_props(hydrated, schema, bound_keys, flavor_groups)
    _cap_list_props(hydrated, schema, bound_keys)

    try:
        validated = validate_props(component, hydrated)
    except ValidationError as exc:
        # Same structural-only detail contract as parse_op_line: field paths
        # + error kinds, never input values.
        details = ";".join(
            f"{'.'.join(str(p) for p in err.get('loc', ()))}:{err.get('type')}"
            for err in exc.errors()[:5]
        )
        return OpResult(error=f"bind_validation_failed:{component}:{details}")
    except Exception as exc:  # defensive
        return OpResult(error=f"bind_validation_failed:{type(exc).__name__}")

    props_out = validated.model_dump(exclude_none=True, mode="json")
    # Selection directive, already applied server-side — not a render prop;
    # keep it off the wire (and out of persisted ui_blocks).
    sel_field = getattr(schema, "selection_field", None)
    if sel_field:
        props_out.pop(sel_field, None)
    hydrated_op: Dict[str, Any] = {
        "op": "add",
        "id": op.get("id"),
        "type": component,
        "props": props_out,
        "v": 2,
    }
    parent = op.get("parent")
    if isinstance(parent, str) and parent:
        hydrated_op["parent"] = parent
    return OpResult(op=hydrated_op)


__all__ = [
    "BindRef",
    "BindingStore",
    "parse_bind_ref",
    "register_selector_transform",
    "resolve_json_pointer",
    "resolve_show_op",
    "selector_extension_keys",
]
