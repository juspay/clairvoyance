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
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Set,
    Type,
    TypeGuard,
    Union,
    get_args,
    get_origin,
)

from pydantic import BaseModel

from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    PRIMITIVE_RENDER_ORDER,
    UI_CATALOG,
    is_data_bound,
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
    "QuickReplies": {
        "+": "qr1:QuickReplies@root",
        "items": [
            {"label": "Yes, confirm"},
            {"label": "No, cancel", "value": "cancel_order_intent"},
            {
                "label": "View your order",
                "action": {
                    "type": "open_url",
                    "url": "https://shop.example/orders/123",
                    "target": "new_tab",
                },
            },
        ],
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
    "KPI": {
        "+": "kpi1:KPI@root",
        "label": "Total calls",
        "value": "2,999",
        "delta": "+12%",
        "trend": "up",
    },
    "MetricCard": {
        "+": "mc1:MetricCard@root",
        "title": "No-answer rate",
        "value": "52%",
        "caption": "1,551 of 2,999 this week",
        "tone": "warning",
    },
    "Sparkline": {"+": "spark1:Sparkline@root", "values": [120, 98, 141, 110, 165]},
    "ProgressBar": {
        "+": "prog1:ProgressBar@root",
        "label": "Confirmed",
        "value": 87,
        "max": 100,
        "tone": "positive",
    },
    "BarChart": {
        "+": "bar1:BarChart@root",
        "title": "Orders by status",
        "data": [
            {"label": "Confirmed", "value": 42},
            {"label": "Cancelled", "value": 8},
            {"label": "No answer", "value": 15},
        ],
    },
    "LineChart": {
        "+": "line1:LineChart@root",
        "title": "Calls per day",
        "data": [
            {"label": "Mon", "value": 120},
            {"label": "Tue", "value": 98},
            {"label": "Wed", "value": 141},
        ],
    },
    "AreaChart": {
        "+": "area1:AreaChart@root",
        "title": "Chat volume",
        "data": [
            {"label": "Wk1", "value": 300},
            {"label": "Wk2", "value": 420},
            {"label": "Wk3", "value": 380},
        ],
    },
    "PieChart": {
        "+": "pie1:PieChart@root",
        "title": "Outcome split",
        "donut": True,
        "data": [
            {"label": "Confirmed", "value": 42},
            {"label": "Cancelled", "value": 8},
            {"label": "Address updated", "value": 12},
        ],
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
    "Lists/carousels — STREAM items progressively: emit the container op "
    "FIRST, then ONE op per item, EACH ON ITS OWN LINE. The server forwards "
    "each line the instant it completes, so items paint one-by-one as you "
    "write them (fast first paint) instead of all landing at the end. You "
    "still pick the items and each item's fields (e.g. the matching variant's "
    "image/price). Example — container, then one compact op per item:\n"
    '  {"+":"car:Carousel@root","snap":true}\n'
    '  {"+":"p1:Tile@car","title":"Dawn","media":{"src":"https://x/1.jpg","alt":"Dawn"}}\n'
    '  {"+":"p2:Tile@car","title":"Dusk","media":{"src":"https://x/2.jpg","alt":"Dusk"}}\n'
    "Never pack a whole list into a single line (e.g. a `repeat` template with "
    "an inline items array) — that holds the first item back until the entire "
    "list is generated, defeating progressive rendering."
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
    '  - For "items in a list", emit the container then ONE op per item, '
    "each on its OWN line (see Lists above) so they stream in progressively "
    "— never compose Card+Image+Text manually, and never pack the whole list "
    "into one line."
)

_EMPTY_BODY = (
    "(No primitives are enabled for this template. The widget will render "
    "nothing — keep responses prose-only.)"
)


# ---------------------------------------------------------------------------
# Data-bound components subsection (catalog v2, RFC-001)
#
# Rendered only when the allowlist contains at least one data_bound
# component. Deliberately terse — the whole subsection stays ≤ ~40 lines.
# Data-bound components are EXCLUDED from the main per-primitive entries:
# the LLM must reach them via `show` ops, never hand-typed `add` props.
# ---------------------------------------------------------------------------

_DATA_BOUND_HEADER = (
    "### Data-bound components\n"
    "\n"
    "These render from TOOL DATA — NEVER hand-type their data props. Emit ONE "
    "`show` op; the server fills props from THIS turn's tool results and "
    "drops the op if a binding cannot be resolved (stale data never renders):\n"
    '  {"op":"show","id":"<id>","component":"<Name>",'
    '"bind":{"<prop>":"$tool:<tool_name>#<json-pointer>"},'
    '"props":{<literal hints>}}\n'
    "- `bind`: prop → `$tool:<tool_name>#<pointer>` (RFC 6901 pointer into "
    "that tool's result; optional `@<tool_use_id>` picks one call in a "
    "multi-call turn).\n"
    "- A `bind` resolves ONLY against a tool call made in THIS turn — if "
    "the needed tool has not run this turn, call it first, then emit the "
    "`show` op.\n"
    "- `props`: small literal hints only (layout, max_items). A key must not "
    "appear in both `bind` and `props`.\n"
    '- Root/parent rules match `add` (root op has id "root", omits parent). '
    "Emit the op in canonical form — the compact `+` shorthand does not "
    "apply to `show`."
)

_DATA_BOUND_EXAMPLE = (
    'Example: {"op":"show","id":"root","component":"ProductGrid",'
    '"bind":{"products":"$tool:search_catalog#/products"},'
    '"props":{"max_items":6,"layout":"carousel"}}'
)

# Plan-as-emission (Phase 2). Rides the data-bound subsection because
# both ship on the same v2 chat surface; the parser (chat/plan.py)
# strips the marker from prose on every session regardless.
_PLAN_INSTRUCTION = (
    "Multi-step turns: when you will call 2+ tools, FIRST emit your plan "
    'as `<plan>["tool_a","tool_b"]</plan>` on its own line — the tool '
    "names you intend to call, in order. Re-emit a new <plan> to revise. "
    "The user sees it as progress steps; never mention the plan in "
    "prose. Skip it for single-tool or no-tool turns."
)


def _render_data_bound_section(names: List[str]) -> str:
    """Render the ``### Data-bound components`` subsection for the
    allowlisted data-bound components (in curated render order)."""
    entries: List[str] = []
    for name in names:
        model = UI_CATALOG[name]
        lines = [f"**{name}** — {_purpose_line(model)}"]
        for fname, fld in model.model_fields.items():
            lines.append(f"  {_render_top_level_field(fname, fld)}")
        entries.append("\n".join(lines))
    return "\n\n".join(
        [_DATA_BOUND_HEADER, *entries, _DATA_BOUND_EXAMPLE, _PLAN_INSTRUCTION]
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
    data_bound_names: List[str] = []
    for name in PRIMITIVE_RENDER_ORDER:
        if name not in allowlist:
            continue
        model = UI_CATALOG.get(name)
        if model is None:
            continue
        # Server-only components (e.g. ProductDetail) are emitted by
        # direct-intent code paths, never by the LLM — no prompt entry in
        # ANY section, though they stay in the allowlist for validation.
        if getattr(model, "server_only", False):
            continue
        # render_ui-only components (e.g. LinkButton) are not authorable
        # via the text channel (parse rejects them), so the text-channel
        # prompt must not teach them either.
        if not getattr(model, "text_channel", True):
            continue
        # Data-bound components (catalog v2) render in their own `show`-op
        # subsection below — never as `add`-style entries the LLM would
        # crib hand-typed props from.
        if is_data_bound(name):
            data_bound_names.append(name)
            continue
        entries.append(_render_entry(name, model))

    body = "\n\n".join(entries) if entries else _EMPTY_BODY
    if data_bound_names:
        body = f"{body}\n\n{_render_data_bound_section(data_bound_names)}"
    return f"{_HEADER}\n\n{body}\n\n{_FOOTER}"


# The render_ui section is flavor-composed: this module owns only the
# flavor-neutral behavioral contract (tool-not-markup, bind-don't-retype,
# decision='no_ui', one-line-after-render); the product vocabulary that
# makes it land — "shoppers", carts, checkout links, variant coaching —
# belongs to the flavor package and is registered here under the flavor's
# catalog group name. Registration rides the same lazy hook as component
# schemas: ``ui_catalog.ensure_group_loaded`` imports the flavor's
# schemas module (which registers its section as a side effect) inside
# ``resolve_allowlist``, and the chat agent resolves its allowlist in
# ``__init__`` — before any prompt splice — so an enabled flavor's
# section is always in place in time.
_RENDER_UI_FLAVOR_SECTIONS: Dict[str, str] = {}
_RENDER_UI_CHIP_DEDUP_EXAMPLES: Dict[str, str] = {}


def register_render_ui_flavor_section(
    group: str,
    section: str,
    chip_dedup_examples: Optional[str] = None,
) -> None:
    """Register a flavor's render_ui prompt section under its catalog
    group (e.g. ``"commerce"``).

    ``section`` REPLACES the generic ``## Showing UI`` block wholesale
    for sessions whose template enables the group — flavor sections
    restate the full contract in their own vocabulary, so concatenating
    them with the generic text would duplicate the rules.
    ``chip_dedup_examples`` (e.g. ``" (Add to cart, View)"``) splices
    into the forced-final chips contract's dedup rule. Process-global,
    additive, idempotent on re-import — the same registration lifecycle
    as ``ui_catalog.register_primitives``."""
    _RENDER_UI_FLAVOR_SECTIONS[group] = section
    if chip_dedup_examples is not None:
        _RENDER_UI_CHIP_DEDUP_EXAMPLES[group] = chip_dedup_examples


_RENDER_UI_SECTION_GENERIC = (
    "## Showing UI (render_ui tool)\n"
    "You show the user UI components ONLY by calling the render_ui "
    "function — never by writing markup, JSON, op lines, or any "
    "<ui_stream> text in your reply. Prose is for words; render_ui is "
    "for UI.\n"
    "- After certain successful tool calls you will be required to call "
    "render_ui exactly once: render a component, or pass decision='no_ui' "
    "with a short reason when showing nothing serves the user better.\n"
    "- Bind data, never retype it: bind=[{prop:'<prop>', "
    "ref:'$tool:<tool_name>#/<json_pointer>'}]. The server fills every "
    "value from THIS turn's tool results.\n"
    "- For a link-only answer render LinkButton with link={label, url} "
    "instead of pasting the URL in prose — the url must be one the "
    "template trusts or one from THIS turn's tool results.\n"
    "- After render_ui succeeds, write at most ONE short line; never "
    "repeat what the UI already shows. The function response tells you "
    "exactly what rendered — use its ids for follow-ups.\n"
)


_QUICK_REPLIES_FORCED_FINAL_TMPL = (
    "- QuickReplies are decided at the END of your turn — never render "
    "them mid-turn. After your final reply you will be asked once more to "
    "call render_ui: respond with QuickReplies (2-4 follow-ups, <=4 words "
    "each, grounded in what you just said) or decision='no_ui' when none "
    "would genuinely help. Never suggest a chip that duplicates an action "
    "already visible on this turn's UI{dedup_examples}.\n"
)


def render_render_ui_section(
    quick_replies_mode: Optional[str] = None,
    flavor_groups: Optional[List[str]] = None,
) -> str:
    """The compact UI section for render_ui-mode sessions (RFC-002): the
    tool schema self-documents the arg shapes, so the prompt carries only
    the behavioral contract — replacing the entire ``<ui_stream>``
    authoring catalog for this template.

    ``flavor_groups`` is the template's ``ui_catalog.enabled_groups``:
    the first group with a registered flavor section (see
    :func:`register_render_ui_flavor_section`) supplies the section body
    and the chips dedup examples; with none registered the generic
    contract above renders.

    The plan instruction must ride along: it normally ships inside the
    data-bound subsection this section REPLACES, and without it the model
    never emits ``<plan>`` — so plan enforcement (which arms off the
    extracted marker) would silently stay dormant in render_ui mode.

    ``quick_replies_mode='forced_final'`` appends the end-of-turn chips
    contract (never mid-turn; the harness runs one forced render_ui cycle
    after the final reply)."""
    groups = flavor_groups or []
    section = next(
        (
            _RENDER_UI_FLAVOR_SECTIONS[g]
            for g in groups
            if g in _RENDER_UI_FLAVOR_SECTIONS
        ),
        _RENDER_UI_SECTION_GENERIC,
    )
    if quick_replies_mode == "forced_final":
        dedup_examples = next(
            (
                _RENDER_UI_CHIP_DEDUP_EXAMPLES[g]
                for g in groups
                if g in _RENDER_UI_CHIP_DEDUP_EXAMPLES
            ),
            "",
        )
        section += _QUICK_REPLIES_FORCED_FINAL_TMPL.format(
            dedup_examples=dedup_examples
        )
    return section + "- " + _PLAN_INSTRUCTION + "\n"


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


__all__ = [
    "register_render_ui_flavor_section",
    "render_primitives_section",
    "render_render_ui_section",
]
