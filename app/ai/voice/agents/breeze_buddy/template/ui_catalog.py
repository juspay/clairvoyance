"""Typed SpecStream / A2UI component catalog + group registry.

Each primitive is a Pydantic ``BaseModel`` describing the ``props`` allowed
on an ``add`` / ``replace`` op for that ``type``. Catalog is the runtime
source of truth — validation, healing, JIT instruction targets, renderer
contracts, and system-prompt rendering all consult it.

The catalog is partitioned into **groups** (``core`` / ``composite`` /
``graphs`` / ``metrics`` / ``forms`` / ``media`` / ``data``). Templates
declare which groups + optional one-offs are enabled per merchant via
``configurations.ui_catalog`` — the LLM never sees primitives that
aren't enabled for its template. Server validation drops disabled ops
with a distinct reason (``primitive_disabled:<type>``) so telemetry can
separate "LLM hallucinated" from "merchant turned off".

Catalog versions: a single ``v1`` URI ships the whole set. Primitives bump
together (see SCALE_ROADMAP.md §"Open questions").

Wire format (mirrors A2UI v0.9 / Vercel json-render SpecStream)::

    {"op":"add","id":"root","type":"Carousel"}
    {"op":"add","id":"p1","type":"Tile","parent":"root","props":{
        "media":{"src":"https://...","alt":"snowboard"},
        "title":"The Complete Snowboard",
        "body":[{"kind":"key_value","key":"Price","value":"₹699.95"}],
        "attributes":[{"label":"Premium","tone":"info"}],
        "actions":[{"label":"View","action":{"type":"to_assistant","msg":"..."}}]
    }}
    {"op":"remove","id":"c2"}

The catalog is fully commerce-agnostic — no Money, no checkout, no
cart shape. Vertical flavor (product cards, cart lines, FAQ answers,
appointment slots, …) lives entirely in the template's
``tool_ui_instructions`` (JIT pattern). Upstream-provided display
strings (e.g. ``"₹699.95"``) flow through ``Text`` / ``key_value``
body rows; locale-aware reformatting is the upstream tool's job.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set, Type, Union

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

CATALOG_VERSION: str = "v1"


# ---------------------------------------------------------------------------
# Actions — embedded inside Button.props.action and Tile actions
# ---------------------------------------------------------------------------


class ToAssistantAction(BaseModel):
    """Re-enter the chat as if the shopper typed ``msg``. Identifier-carrying
    messages are how row-scoped buttons round-trip context (e.g. include the
    product id in the message).

    ``display`` lets the shopper see a clean label while ``msg`` keeps the
    identifier (e.g. ``msg='Add gid://shopify/.../12345 to cart'``,
    ``display='Add this product to my cart'``). When absent the widget
    falls back to a heuristic GID-stripper for the user bubble; backend
    always receives the full ``msg``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["to_assistant"] = "to_assistant"
    msg: str = Field(
        ..., min_length=1, description="Message to deliver back to the agent."
    )
    display: Optional[str] = Field(
        None,
        description=(
            "Optional user-facing label shown in the chat bubble instead of "
            "``msg``. Use whenever ``msg`` contains technical context (GIDs, "
            "UUIDs, raw payloads) that the shopper shouldn't see."
        ),
    )


class OpenUrlAction(BaseModel):
    """Open ``url`` in a new tab. Server-provided URLs only."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["open_url"] = "open_url"
    url: HttpUrl


ActionUnion = Union[ToAssistantAction, OpenUrlAction]


# ---------------------------------------------------------------------------
# Base — forbid unknown props so the healer can deterministically strip them
# ---------------------------------------------------------------------------


class _CatalogBase(BaseModel):
    """All primitive schemas inherit this. ``extra='forbid'`` makes Pydantic
    raise on unknown keys; the healer's ``_rule_strip_unknown_props`` rule
    catches and removes them before validation runs."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Layout primitives (group: core)
# ---------------------------------------------------------------------------


class Stack(_CatalogBase):
    """Vertical stack container. Children added via subsequent ``add`` ops
    with this op's id as ``parent``."""

    gap: Optional[Literal["sm", "md", "lg"]] = Field(
        None, description="Spacing between children. Defaults to md."
    )


class Row(_CatalogBase):
    """Horizontal row container."""

    gap: Optional[Literal["sm", "md", "lg"]] = None
    align: Optional[Literal["start", "center", "end", "between"]] = None


class Card(_CatalogBase):
    """Bordered card surface. Use for freeform composition. PREFER ``Tile``
    when rendering "items in a list" — Tile is uniform by construction."""

    variant: Optional[Literal["default", "muted", "highlighted"]] = None


class CardHeader(_CatalogBase):
    """Heading row inside a Card. Usually contains a Text and optional Tag."""

    title: str = Field(..., description="Heading text.")
    subtitle: Optional[str] = None


# ---------------------------------------------------------------------------
# Content primitives (group: core)
# ---------------------------------------------------------------------------


class Image(_CatalogBase):
    """Product image / illustration. Renderer applies object-fit cover."""

    src: HttpUrl
    alt: str = Field(..., description="Alt text — never empty.")
    aspect: Optional[Literal["1:1", "4:3", "16:9"]] = "1:1"


class Text(_CatalogBase):
    """Prose, captions, body copy. Use the tool response's pre-formatted
    display string verbatim for things like prices — never invent or
    reformat locale-specific values."""

    text: str = Field(..., min_length=1)
    variant: Optional[Literal["body", "caption", "heading", "muted"]] = "body"


class Carousel(_CatalogBase):
    """Horizontal-scrolling list. Children typically Tiles (preferred) or Cards."""

    snap: Optional[bool] = True


class Tag(_CatalogBase):
    """Chip-shaped attribute. Use for categorical attributes, not for
    descriptive prose (that's Text)."""

    text: str = Field(..., min_length=1)
    tone: Optional[Literal["neutral", "positive", "warning", "info"]] = "neutral"


# ---------------------------------------------------------------------------
# Action primitives (group: core)
# ---------------------------------------------------------------------------


class Button(_CatalogBase):
    """Single button. ``action`` MUST validate as ``ActionUnion``."""

    label: str = Field(..., min_length=1)
    action: ActionUnion
    variant: Optional[Literal["primary", "secondary", "ghost"]] = "primary"

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v: Any) -> Any:
        return v


class Buttons(_CatalogBase):
    """Inline row of buttons."""

    align: Optional[Literal["start", "center", "end"]] = "start"


# ---------------------------------------------------------------------------
# Data primitives (group: core)
# ---------------------------------------------------------------------------


class Table(_CatalogBase):
    """Tabular data. ``columns`` is a list of header labels; ``rows`` is a
    list of equal-length string lists. NEVER render arrays inside Text."""

    columns: List[str] = Field(..., min_length=1)
    rows: List[List[str]] = Field(default_factory=list)

    @field_validator("rows")
    @classmethod
    def _rows_match_columns(cls, v: List[List[str]], info: Any) -> List[List[str]]:
        cols = info.data.get("columns") or []
        for i, row in enumerate(v):
            if len(row) != len(cols):
                raise ValueError(f"row {i} has {len(row)} cells; expected {len(cols)}")
        return v


# ---------------------------------------------------------------------------
# Typed primitives — the "new 3" that motivated the SpecStream migration
# (group: core)
# ---------------------------------------------------------------------------


class MessageSeverity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    success = "success"


class MessageResolution(str, Enum):
    """Drives a fixed UI affordance — closed enum, no surprises.

    * ``recoverable`` — silent retry; renderer shows nothing.
    * ``requires_user_input`` — render an inline form bound to ``param``.
    * ``requires_user_confirmation`` — render a confirm CTA.
    * ``unrecoverable`` — render a Handoff with a continue_url.
    """

    recoverable = "recoverable"
    requires_user_input = "requires_user_input"
    requires_user_confirmation = "requires_user_confirmation"
    unrecoverable = "unrecoverable"


class Message(_CatalogBase):
    """Spec-driven user notification. Severity + resolution mechanically
    drive UI — the LLM never picks visual treatment."""

    severity: MessageSeverity
    resolution: MessageResolution
    content: str = Field(..., min_length=1)
    param: Optional[str] = Field(
        None,
        description=(
            "JSONPath into the underlying tool payload — used when "
            "``resolution == requires_user_input`` to bind the inline form."
        ),
    )


class HandoffLifecycle(str, Enum):
    """How the widget transitions when the user takes the handoff.

    * ``popup`` — ``window.open`` and poll a completion token (10s default).
    * ``same_tab`` — set ``location.href`` directly.
    * ``polling`` — widget shows a shimmer and polls a completion endpoint
      without leaving the chat.
    """

    popup = "popup"
    same_tab = "same_tab"
    polling = "polling"


class Handoff(_CatalogBase):
    """Typed handoff URL. Replaces ad-hoc ``OpenUrl`` button actions where
    the URL has lifecycle semantics (checkout, auth, 3DS, escalation, …)."""

    reason: str = Field(
        ...,
        description=(
            "Stable string identifying the handoff intent — e.g. "
            "'checkout', 'auth', '3ds', 'compliance_review', 'escalation'."
        ),
    )
    label: str = Field(..., min_length=1, description="Human-visible CTA text.")
    url: HttpUrl
    lifecycle: HandoffLifecycle


# ---------------------------------------------------------------------------
# Composite primitives (group: composite)
#
# Tile is the generic uniform "item in a list" primitive. The renderer
# owns the layout (media → eyebrow → title → subtitle → body → attributes →
# actions); the LLM fills slots. Use Tile for product cards, cart lines,
# order summaries, FAQ answers, appointments — anywhere a uniform shape
# matters more than per-item composition flexibility.
# ---------------------------------------------------------------------------


class TileMedia(BaseModel):
    """Image-ish thing at the top of a Tile. Same fields as the Image
    primitive but nested as a structured slot instead of a child op."""

    model_config = ConfigDict(extra="forbid")

    src: HttpUrl
    alt: str = Field(..., min_length=1, description="Alt text — never empty.")
    aspect: Optional[Literal["1:1", "4:3", "16:9"]] = "1:1"


class TileBodyKind(str, Enum):
    """Polymorphic body item discriminator. Adding a new kind is a deliberate
    catalog bump — closed set keeps render deterministic."""

    text = "text"
    key_value = "key_value"
    message = "message"


class TileBodyItem(BaseModel):
    """One polymorphic row in a Tile's body.

    Set ``kind`` plus the matching field for that kind; other fields are
    ignored. Pydantic's ``extra='forbid'`` rejects mistakes.

    Examples::

        {"kind": "text", "text": "Limited edition release"}
        {"kind": "key_value", "key": "Price", "value": "₹699.95"}
        {"kind": "key_value", "key": "Material", "value": "Wood + epoxy"}
        {"kind": "message", "message": {"severity":"warning", "resolution":"recoverable", "content":"Low stock"}}

    Numeric-money values flow through ``key_value`` (or ``text``) using the
    upstream tool's pre-formatted display string — the catalog stays
    commerce-agnostic.
    """

    model_config = ConfigDict(extra="forbid")

    kind: TileBodyKind
    text: Optional[str] = Field(None, description="Set when kind == text.")
    key: Optional[str] = Field(None, description="Set when kind == key_value.")
    value: Optional[str] = Field(None, description="Set when kind == key_value.")
    message: Optional[Message] = Field(None, description="Set when kind == message.")


class TileAttribute(BaseModel):
    """One chip in the attribute row. Maps to a Tag at render time."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    tone: Optional[Literal["neutral", "positive", "warning", "info"]] = "neutral"


class TileAction(BaseModel):
    """One button in the action row at the bottom of a Tile."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1)
    action: ActionUnion
    variant: Optional[Literal["primary", "secondary", "ghost"]] = "primary"


class Tile(_CatalogBase):
    """Generic uniform composite — slot-filled card.

    Renderer owns the layout (top → bottom):

      ┌─────────────────────┐
      │       media         │   optional, fixed-aspect at top
      ├─────────────────────┤
      │ EYEBROW             │   optional, small label
      │ Title               │   required, prominent
      │ subtitle            │   optional, secondary
      │ body rows…          │   polymorphic content
      │ [attr1] [attr2] …   │   optional chip row
      │ [Action]  [Action]  │   button row at bottom
      └─────────────────────┘

    Every Tile in the same parent (typically a Carousel or Stack) renders
    the SAME shape — picking Tile over hand-composed Card+Image+Text+Tag
    children guarantees visual uniformity even if the LLM forgets a slot
    on some items. Missing slots simply don't render — the layout stays
    intact.

    Density tunes padding + gap tokens:
      * ``compact``  — tight rows, list-line use case
      * ``default``  — standard product-card use case
      * ``spacious`` — order summary / confirm card use case

    Layout chooses media placement:
      * ``card``  — media on TOP (default); use for carousel/grid items
        and detail views.
      * ``row``   — media on LEFT as a small thumbnail; use for line items
        in a vertical list (cart, transactions, inbox, history). In a
        Stack of row-layout Tiles, per-tile card chrome is suppressed in
        favour of hairline separators between siblings — the list reads
        as a single coherent block, not a column of cards.
    """

    media: Optional[TileMedia] = None
    eyebrow: Optional[str] = Field(
        None, description="Small uppercase label above title (e.g. 'NEW', 'SALE')."
    )
    title: str = Field(..., min_length=1, description="The Tile's primary heading.")
    subtitle: Optional[str] = Field(
        None, description="Optional secondary line under title."
    )
    trailing: Optional[str] = Field(
        None,
        description=(
            "Prominent right-aligned value at the title row. Used most often "
            "in ``layout='row'`` to surface a price, total, count, or status "
            "without needing a labeled key_value body row. Typically a short "
            "display string (currency, count, percentage)."
        ),
    )
    body: List[TileBodyItem] = Field(
        default_factory=list,
        description="Polymorphic content rows (text / money / key_value / message).",
    )
    attributes: List[TileAttribute] = Field(
        default_factory=list,
        description="Optional chip row above the actions row.",
    )
    actions: List[TileAction] = Field(
        default_factory=list,
        description="Optional button row at the bottom of the Tile.",
    )
    density: Optional[Literal["compact", "default", "spacious"]] = "default"
    layout: Optional[Literal["card", "row"]] = "card"


# ---------------------------------------------------------------------------
# Catalog manifest + group registry
# ---------------------------------------------------------------------------


UI_CATALOG: Dict[str, Type[_CatalogBase]] = {
    # Layout (core)
    "Stack": Stack,
    "Row": Row,
    "Card": Card,
    "CardHeader": CardHeader,
    # Content (core)
    "Image": Image,
    "Text": Text,
    "Carousel": Carousel,
    "Tag": Tag,
    # Actions (core)
    "Button": Button,
    "Buttons": Buttons,
    # Data (core)
    "Table": Table,
    # Typed (core)
    "Message": Message,
    "Handoff": Handoff,
    # Composite
    "Tile": Tile,
}


# Group registry — each primitive belongs to exactly one group. Adding a
# new group means adding a key here AND, for system-prompt rendering, an
# entry in ``PRIMITIVE_RENDER_ORDER`` so the LLM sees a sensible order.
#
# Future groups have empty member lists today; populating them is the
# extension path:
#   1. Add the new primitive's Pydantic schema above
#   2. Register it in UI_CATALOG
#   3. Append it to the matching group's list
#   4. Add render-order entry
#   5. Add a Svelte component + UiNode dispatch on the widget side
#   6. Templates opt in via ``ui_catalog.enabled_groups``
PRIMITIVE_GROUPS: Dict[str, List[str]] = {
    "core": [
        "Stack",
        "Row",
        "Card",
        "CardHeader",
        "Image",
        "Text",
        "Carousel",
        "Tag",
        "Button",
        "Buttons",
        "Table",
        "Message",
        "Handoff",
    ],
    "composite": [
        "Tile",
    ],
    # Reserved for future expansion — schemas + widget components land
    # behind these group flags so existing templates aren't affected.
    "graphs": [
        # "LineChart", "BarChart", "PieChart", "AreaChart"
    ],
    "metrics": [
        # "KPI", "Sparkline", "MetricCard", "ProgressBar"
    ],
    "forms": [
        # "TextInput", "Select", "Checkbox", "RadioGroup"
        # — required for `requires_user_input` Messages in S3.3
    ],
    "media": [
        # "Video", "Audio", "Embed"
    ],
    "data": [
        # "DataGrid", "Pivot", "TreeView"
    ],
}


# Order in which primitives appear in the system-prompt rendering. Listed
# composite-first so the LLM defaults to Tile for list items, then core
# building blocks for freeform composition.
PRIMITIVE_RENDER_ORDER: List[str] = [
    # Composite first — preferred for list items
    "Tile",
    # Layout containers
    "Carousel",
    "Stack",
    "Row",
    "Card",
    "CardHeader",
    # Content
    "Image",
    "Text",
    "Tag",
    # Actions
    "Button",
    "Buttons",
    "Handoff",
    # Data
    "Table",
    # Notifications
    "Message",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def group_for(type_name: str) -> Optional[str]:
    """Return the group name a primitive belongs to, or None if unknown."""
    for group_name, members in PRIMITIVE_GROUPS.items():
        if type_name in members:
            return group_name
    return None


def is_known_type(type_name: str) -> bool:
    """True when ``type_name`` is a registered primitive (regardless of
    whether any template has it enabled)."""
    return type_name in UI_CATALOG


def resolve_allowlist(
    *,
    enabled_groups: Optional[List[str]] = None,
    enabled_primitives: Optional[List[str]] = None,
    disabled_primitives: Optional[List[str]] = None,
) -> Set[str]:
    """Resolve a template's UI-catalog config into a concrete allowlist set.

    Precedence (highest → lowest):
      1. ``disabled_primitives`` always wins (remove from the result).
      2. ``enabled_primitives`` adds one-offs even if their group isn't
         in ``enabled_groups``.
      3. ``enabled_groups`` expands to all primitives in those groups.

    Unknown group names and primitive names are silently ignored so a
    stale template doesn't crash a session.

    When ``enabled_groups`` is None (the absent-config case), the default
    is ``{"core"}`` — keeps backward compatibility with templates that
    pre-date ``configurations.ui_catalog``.
    """
    if enabled_groups is None and not enabled_primitives:
        enabled_groups = ["core"]
    enabled_groups = enabled_groups or []
    enabled_primitives = enabled_primitives or []
    # Use a distinct variable name for the set form so pyrefly can keep the
    # parameter's `Optional[List[str]]` type intact (reassigning would shift
    # the binding to `set[str]` and trip downstream type checks).
    disabled_set: Set[str] = set(disabled_primitives or [])

    result: Set[str] = set()
    for group in enabled_groups:
        for prim in PRIMITIVE_GROUPS.get(group, []):
            result.add(prim)
    for prim in enabled_primitives:
        if prim in UI_CATALOG:
            result.add(prim)
    result -= disabled_set
    # Drop anything that's no longer a registered primitive (defensive
    # against templates referencing names that the catalog removed in a
    # later bump).
    result &= set(UI_CATALOG.keys())
    return result


def validate_props(type_name: str, props: Dict[str, Any]) -> _CatalogBase:
    """Validate ``props`` against the primitive's Pydantic schema.

    Raises ``pydantic.ValidationError`` on mismatch — caller decides whether
    to drop the op, heal it, or surface to the LLM.
    """
    schema = UI_CATALOG[type_name]
    return schema.model_validate(props or {})


__all__ = [
    "CATALOG_VERSION",
    "UI_CATALOG",
    "PRIMITIVE_GROUPS",
    "PRIMITIVE_RENDER_ORDER",
    "group_for",
    "is_known_type",
    "resolve_allowlist",
    "validate_props",
    "ToAssistantAction",
    "OpenUrlAction",
    "ActionUnion",
    "Stack",
    "Row",
    "Card",
    "CardHeader",
    "Image",
    "Text",
    "Carousel",
    "Tag",
    "Button",
    "Buttons",
    "Table",
    "Message",
    "MessageSeverity",
    "MessageResolution",
    "Handoff",
    "HandoffLifecycle",
    "Tile",
    "TileMedia",
    "TileBodyKind",
    "TileBodyItem",
    "TileAttribute",
    "TileAction",
]
