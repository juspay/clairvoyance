"""Render the ``## Available primitives`` section of the system prompt.

The LLM sees a per-template, per-merchant slice of the primitive catalog.
At session start the chat agent resolves the allowlist (see
``UiCatalogConfig`` + ``resolve_allowlist``) and the builder splices the
output of :func:`render_primitives_section` into the template's
``system_prompt`` wherever the literal placeholder
``{{ui_primitives_section}}`` appears.

The rendering is deliberately introspection-driven so the prompt always
matches the runtime schema — adding a new primitive (or a new field on
an existing one) updates the prompt automatically; the catalog stays
the single source of truth.

Format is terse — the LLM reads this every turn:

    **Tile** — Generic uniform composite — slot-filled card.
      Props:
        media?: {src*: HttpUrl, alt*: str, ...}
        title*: str
        ...
      Example: {"op":"add",...}

Asterisks mark required fields; question marks mark optional fields.
``Literal[...]`` enums render as ``"a"|"b"``; nested Pydantic submodels
render with their own brace-shape; ``List[T]`` renders as ``[T-shape]``.
"""

from __future__ import annotations

import enum
import json
import typing
from functools import lru_cache
from typing import Any, Dict, List, Set, Type, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    PRIMITIVE_RENDER_ORDER,
    UI_CATALOG,
)

# ---------------------------------------------------------------------------
# Hard-coded minimal-but-realistic examples per primitive, shown in the
# compact wire form (A3). The LLM cribs from these for the JIT few-shot
# pattern, so each must round-trip through ``expand_compact_op`` ->
# ``validate_props`` cleanly. Keep them small — one op per primitive.
# ---------------------------------------------------------------------------


_EXAMPLES: Dict[str, Dict[str, Any]] = {
    "Tile": {
        "+": "p1:Tile@root",
        "media": {"src": "https://x/y.jpg", "alt": "snowboard"},
        "title": "The Complete Snowboard",
        "body": [{"kv": ["Price", "₹699.95"]}],
        "attributes": [{"label": "Premium", "tone": "info"}],
        "actions": [
            {
                "label": "View",
                "action": {"type": "to_assistant", "msg": "Tell me about <id>"},
            }
        ],
    },
    "Carousel": {"+": "c1:Carousel@root", "snap": True},
    "Stack": {"+": "s1:Stack@root", "gap": "md"},
    "Card": {"+": "card1:Card@root", "variant": "default"},
    "Image": {
        "+": "img1:Image@card1",
        "src": "https://x/y.jpg",
        "alt": "snowboard",
        "aspect": "4:3",
    },
    "Text": {
        "+": "t1:Text@card1",
        "text": "Limited edition release",
        "variant": "body",
    },
    "Tag": {"+": "tag1:Tag@card1", "text": "New", "tone": "info"},
    "Button": {
        "+": "btn1:Button@card1",
        "label": "View",
        "action": {"type": "to_assistant", "msg": "Tell me more about <id>"},
        "variant": "primary",
    },
    "Handoff": {
        "+": "h1:Handoff@root",
        "reason": "<intent>",
        "label": "Continue",
        "url": "https://example/handoff/abc",
        "lifecycle": "popup",
    },
    "Message": {
        "+": "msg1:Message@root",
        "severity": "warning",
        "resolution": "recoverable",
        "content": "Temporary issue retrieving data.",
    },
    "Table": {
        "+": "tbl1:Table@root",
        "columns": ["Item", "Qty", "Total"],
        "rows": [["Item A", "1", "100"]],
    },
}


# ---------------------------------------------------------------------------
# Type rendering — extract a terse signature from a Pydantic field annotation
# ---------------------------------------------------------------------------


# Pydantic adds a metadata layer around HttpUrl that's awkward to walk.
# We special-case it (and other common types) for cleaner output.
_TYPE_NAME_MAP: Dict[Any, str] = {
    str: "str",
    int: "int",
    float: "float",
    bool: "bool",
}


def _is_pydantic_model(t: Any) -> TypeGuard[Type[BaseModel]]:
    """TypeGuard so callers' subsequent uses of ``t`` narrow to
    ``Type[BaseModel]`` for static analysers without needing a cast."""
    return isinstance(t, type) and issubclass(t, BaseModel)


def _render_literal(args: tuple) -> str:
    """Render Literal["a","b"] as ``"a"|"b"``."""
    return "|".join(f'"{a}"' if isinstance(a, str) else str(a) for a in args)


def _render_type(t: Any, *, recurse_models: bool = True) -> str:
    """Best-effort terse rendering of a Pydantic field annotation.

    The renderer doesn't aim to be a full type printer — it produces
    something terse enough for the system prompt while still hinting at
    nested shape. ``recurse_models=False`` short-circuits nested model
    expansion (used to render ``List[Model]`` as ``[Model-shape]``
    without recursing twice).
    """
    if t is type(None):
        return "null"
    if t in _TYPE_NAME_MAP:
        return _TYPE_NAME_MAP[t]

    origin = get_origin(t)
    args = get_args(t)

    # Optional[T] / Union[T, None] — unwrap and render inner.
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _render_type(non_none[0], recurse_models=recurse_models)
        return "|".join(
            _render_type(a, recurse_models=recurse_models) for a in non_none
        )

    # Literal["a", "b"]
    if origin is typing.Literal:
        return _render_literal(args)

    # List[T] → [T-shape]
    if origin in (list, List):
        if not args:
            return "[]"
        inner = args[0]
        if _is_pydantic_model(inner):
            return f"[{_render_model_inline(inner)}]"
        return f"[{_render_type(inner, recurse_models=recurse_models)}]"

    # Dict[K, V]
    if origin in (dict, Dict):
        if len(args) == 2:
            return f"{{{_render_type(args[0])}: {_render_type(args[1])}}}"
        return "dict"

    # Pydantic model — render inline shape.
    if _is_pydantic_model(t):
        if recurse_models:
            return _render_model_inline(t)
        return t.__name__

    # Enum subclass — render its values as a Literal-style union so the LLM
    # sees the actual allowed strings rather than the opaque class name.
    if isinstance(t, type) and issubclass(t, enum.Enum):
        return "|".join(
            f'"{m.value}"' if isinstance(m.value, str) else str(m.value) for m in t
        )

    # HttpUrl, custom types — fall back to class name.
    if isinstance(t, type):
        return t.__name__

    # Anything else — string-cast and trim ``typing.`` prefix.
    s = str(t)
    if s.startswith("typing."):
        s = s[len("typing.") :]
    return s


def _render_model_inline(model: Type[BaseModel]) -> str:
    """Render a nested model as ``{field*: type, optional?: type}``.

    Stops recursing into further nested models — second-level nesting
    just shows the field names without their full sub-shape, to keep
    the prompt readable. ``Message``-inside-``TileBodyItem`` shows as
    ``message?: {severity*, resolution*, content*, param?}`` which is
    what the spec calls for.
    """
    parts: List[str] = []
    for fname, field in model.model_fields.items():
        required = field.is_required()
        suffix = "*" if required else "?"
        ann = field.annotation
        # For doubly-nested models, just show the field-name shape (one
        # level of detail) — no annotation. ``{amount*, currency*}``.
        inner_origin = get_origin(ann)
        inner_args = get_args(ann)
        non_none = (
            [a for a in inner_args if a is not type(None)]
            if inner_origin is Union
            else []
        )
        target = non_none[0] if len(non_none) == 1 else ann
        if _is_pydantic_model(target):
            parts.append(f"{fname}{suffix}: {_render_nested_shape(target)}")
        else:
            parts.append(f"{fname}{suffix}: {_render_type(ann, recurse_models=False)}")
    return "{" + ", ".join(parts) + "}"


def _render_nested_shape(model: Type[BaseModel]) -> str:
    """One-level-deep summary: ``{field*, other?}`` — just names + req markers.

    Used for second-level nesting so the prompt doesn't explode. Caller
    has already rendered the outer model's field name; this fills the
    type column.
    """
    parts: List[str] = []
    for fname, field in model.model_fields.items():
        parts.append(f"{fname}{'*' if field.is_required() else '?'}")
    return "{" + ", ".join(parts) + "}"


def _render_top_level_field(fname: str, field: Any) -> str:
    """Render one ``Name: type`` line for a top-level primitive field."""
    required = field.is_required()
    suffix = "*" if required else "?"
    ann = field.annotation

    # Unwrap Optional[T] for clearer top-level output.
    origin = get_origin(ann)
    args = get_args(ann)
    if origin is Union:
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            ann = non_none[0]
            origin = get_origin(ann)
            args = get_args(ann)

    # List[T] → ``[shape]``
    if origin in (list, List) and args:
        inner = args[0]
        if _is_pydantic_model(inner):
            return f"{fname}{suffix}: [{_render_model_inline(inner)}]"
        return f"{fname}{suffix}: [{_render_type(inner)}]"

    # Top-level nested model — render inline shape.
    if _is_pydantic_model(ann):
        return f"{fname}{suffix}: {_render_model_inline(ann)}"

    # Literal at top level (e.g. density).
    if origin is typing.Literal:
        return f"{fname}{suffix}: {_render_literal(args)}"

    # Plain scalar / HttpUrl / Enum.
    return f"{fname}{suffix}: {_render_type(ann)}"


# ---------------------------------------------------------------------------
# Per-primitive entry
# ---------------------------------------------------------------------------


def _purpose_line(model: Type[BaseModel]) -> str:
    """Extract the first non-empty line of the model's docstring."""
    doc = model.__doc__ or ""
    for line in doc.split("\n"):
        line = line.strip()
        if line:
            return line
    return ""


def _render_entry(name: str, model: Type[BaseModel]) -> str:
    """Render one primitive's entry: name, purpose, props, example."""
    lines: List[str] = []
    lines.append(f"**{name}** — {_purpose_line(model)}")

    fields = model.model_fields
    if fields:
        lines.append("  Props:")
        for fname, field in fields.items():
            lines.append(f"    {_render_top_level_field(fname, field)}")
    else:
        lines.append("  Props: (none)")

    example = _EXAMPLES.get(name)
    if example is not None:
        lines.append(f"  Example: {json.dumps(example, separators=(',', ':'))}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level section
# ---------------------------------------------------------------------------


_HEADER = (
    "## Available primitives\n"
    "\n"
    "Use ONLY these primitive types in <ui_stream> ops. The server drops "
    "unknown or disabled types silently. Asterisk (*) marks required props.\n"
    "\n"
    "Emit each op in COMPACT wire form (fewer tokens):\n"
    '  add:     {"+":"<id>:<Type>@<parent>", <prop>:<val>, ...}   '
    '(props are top-level keys; a root op drops "@<parent>")\n'
    '  replace: {"~":"<id>", <prop>:<val>, ...}\n'
    '  remove:  {"-":"<id>"}\n'
    '  body key/value row: {"kv":["Label","Value"]}\n'
    "Each Example below uses this form. The canonical "
    '{"op":"add","id":...,"type":...,"props":{...}} shape is also accepted.\n'
    "\n"
    "Lists — render N similar items with ONE element, not N (far fewer tokens):\n"
    '  {"+":"tile:Tile@root", "repeat":{"items":[<rows>],"key":"id"}, '
    '"title":{"$item":"title"}, "media":{"src":{"$item":"image"}}}\n'
    "  `repeat.items` is your data array — YOU pick the rows and which fields "
    "to surface (so per-item choices like the matching variant's image/price "
    'still apply); `{"$item":"<field>"}` binds that row\'s value (dotted '
    "paths ok). The server expands it to one op per row, so all rows render."
)

_FOOTER = (
    "Action shape (embedded inside Button/Tile/Handoff):\n"
    '  {type:"to_assistant", msg*: string}  — re-enter the chat as if '
    "the user typed `msg`\n"
    '  {type:"open_url", url*: HttpUrl}     — open a URL in a new tab\n'
    "\n"
    "Composition rules:\n"
    '  - Root id is always "root"; root op omits `parent`.\n'
    "  - All non-root `add` ops MUST have `parent`.\n"
    "  - `replace` swaps props on an existing id; `remove` deletes a node.\n"
    '  - For "items in a list", emit ONE Tile per item via a `repeat` '
    "template (see Lists above) — never compose Card+Image+Text manually."
)

_EMPTY_BODY = (
    "(No primitives are enabled for this template. The widget will render "
    "nothing — keep responses prose-only.)"
)


@lru_cache(maxsize=64)
def _render_primitives_section_cached(allowlist_key: frozenset) -> str:
    """Pure render keyed by a hashable allowlist (D1, see
    ``docs/widget/UI_FAST_RELIABLE_GENERIC_PLAN.md``).

    The catalog (``UI_CATALOG`` / ``PRIMITIVE_RENDER_ORDER`` / ``_EXAMPLES``)
    is static at module load, so the rendered section is a deterministic
    function of the allowlist — safe to memoise for the process lifetime.
    Keyed by ``frozenset`` so templates sharing a UI allowlist share one
    rendered section (and one Pydantic-introspection pass), instead of
    re-walking the catalog on every ``_splice_ui_primitives`` call (up to
    several per turn).
    """
    allowlist = set(allowlist_key)
    entries: List[str] = []
    for name in PRIMITIVE_RENDER_ORDER:
        if name not in allowlist:
            continue
        model = UI_CATALOG.get(name)
        if model is None:
            continue
        entries.append(_render_entry(name, model))

    body = "\n\n".join(entries) if entries else _EMPTY_BODY
    return f"{_HEADER}\n\n{body}\n\n{_FOOTER}"


def render_primitives_section(allowlist: Set[str]) -> str:
    """Render the ``## Available primitives`` section for an allowlist.

    Walks ``PRIMITIVE_RENDER_ORDER`` and emits one entry per primitive
    that is both in the catalog and in ``allowlist``. Names appearing in
    the allowlist but not in the render order are silently skipped — the
    render order is the curated, human-tuned sequence the LLM should
    read.

    An empty allowlist returns a section with the header + a short
    "nothing enabled" note + the footer, so the prompt still parses and
    the LLM understands the situation rather than crashing on missing
    text.

    Memoised by allowlist (see :func:`_render_primitives_section_cached`):
    the section is rebuilt from the static catalog once per distinct
    allowlist, not per turn.
    """
    return _render_primitives_section_cached(frozenset(allowlist))


__all__ = ["render_primitives_section"]
