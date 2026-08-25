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

Flavor groups (e.g. ``commerce``) are **lazy**: their schemas live under
``breeze_buddy/assist/<flavor>/`` and are imported — and self-registered
via :func:`register_primitives` — only when :func:`resolve_allowlist`
sees a template that enables that group (see ``LAZY_GROUPS`` +
:func:`ensure_group_loaded`). A process that never resolves a
flavor-enabled template never imports the flavor package.

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

This module is fully commerce-agnostic — no Money, no checkout, no
cart shape. Free-form vertical flavor (FAQ answers, appointment slots,
…) lives in the template's ``tool_ui_instructions`` (JIT pattern);
typed flavor components (the catalog-v2 commerce set) live in
``assist/<flavor>/schemas.py`` and lazy-register through the hooks
below. Upstream-provided display strings (e.g. ``"₹699.95"``) flow
through ``Text`` / ``key_value`` body rows; locale-aware reformatting
is the upstream tool's job.
"""

from __future__ import annotations

import importlib
from enum import Enum
from typing import (
    Annotated,
    Any,
    ClassVar,
    Dict,
    List,
    Literal,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
)

from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    UrlConstraints,
    field_validator,
    model_validator,
)

from app.core.logger import logger

CATALOG_VERSION: str = "v1"

# Wire value a catalog-v2-capable widget sends at session create (RFC-001
# §3.4). Sessions without it are v1: the server prunes data-bound components
# from the allowlist, so neither the prompt section nor `show`-op rendering
# ever reaches a v1 client.
CATALOG_VERSION_V2: str = "v2"


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


# Schemes a click action may open. ``mailto:`` / ``tel:`` hand off to the
# OS mail/phone handler — support-contact CTAs ("email us", "call us") are
# a top real-traffic emission and used to drop on HttpUrl's http/https-only
# rule. ``javascript:``/``data:`` and everything else stay rejected.
# Display assets (``Image.src``, ``Icon.url``) remain HttpUrl-only.
OpenableUrl = Annotated[
    AnyUrl, UrlConstraints(allowed_schemes=["http", "https", "mailto", "tel"])
]


class OpenUrlAction(BaseModel):
    """Open ``url``. Server-provided URLs only (http/https/mailto/tel).

    ``target`` controls where it opens: ``new_tab`` (default — preserves the
    historical behavior) or ``same_tab`` to navigate the current page (e.g.
    a checkout redirect). ``mailto:``/``tel:`` URLs open the OS handler
    regardless of target."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["open_url"] = "open_url"
    url: OpenableUrl
    target: Literal["new_tab", "same_tab"] = Field(
        "new_tab",
        description=(
            "Where to open the URL. ``new_tab`` (default) opens a new tab; "
            "``same_tab`` navigates the current page."
        ),
    )


ActionUnion = Union[ToAssistantAction, OpenUrlAction]


class Icon(BaseModel):
    """A small icon shown alongside a quick-reply chicklet. Server-provided
    URLs only (http/https), same posture as ``Image.src``."""

    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    alt: Optional[str] = Field(
        None, description="Accessible label for the icon. Optional."
    )


# ---------------------------------------------------------------------------
# Base — forbid unknown props so the healer can deterministically strip them
# ---------------------------------------------------------------------------


class _CatalogBase(BaseModel):
    """All primitive schemas inherit this. ``extra='forbid'`` makes Pydantic
    raise on unknown keys; the healer's ``_rule_strip_unknown_props`` rule
    catches and removes them before validation runs.

    ``data_bound`` (catalog-v2, RFC-001) marks components whose props are
    hydrated server-side from tool results via ``show`` ops — the LLM never
    hand-types their data. False for every free-form primitive; the
    commerce components override it to True. Class-level metadata only —
    never a wire field.
    """

    model_config = ConfigDict(extra="forbid")

    data_bound: ClassVar[bool] = False

    # Server-authored components (e.g. ProductDetail): validated by the
    # allowlist/resolver like any other, but NEVER rendered into the LLM
    # prompt — only server code (direct intents) emits them.
    server_only: ClassVar[bool] = False

    # False = NOT authorable via the in-band <ui_stream> text channel
    # (parse_op_line rejects the add with ``render_ui_only:<type>``) and
    # never rendered into the text-channel prompt. For components whose
    # trust checks live in the render_ui function path (e.g. LinkButton's
    # URL verification) — a hand-typed text op would bypass them.
    text_channel: ClassVar[bool] = True

    # Name of this component's selection-DIRECTIVE field (e.g. ProductGrid's
    # ``items``): applied by ``_select_list_props`` over bound lists, then
    # stripped from the hydrated op — never a render prop. None = the
    # component has no selection directive (a same-named CONTENT field, like
    # QuickReplies.items, is untouched).
    selection_field: ClassVar[Optional[str]] = None

    # Model-authored LITERAL fields on a data-bound component (e.g.
    # OrderStatus's transcribed ETA). Accepted ONLY via the render_ui
    # function path, and ONLY after the flavor pack's
    # ``verify_literal_fields`` trust gate passed them (anchoring against
    # this turn's tool payloads). A text-channel ``show`` op naming one
    # drops at parse — the gate must not be bypassable in-band.
    literal_fields: ClassVar[Tuple[str, ...]] = ()


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


class QuickReplyItem(BaseModel):
    """One pill in a ``QuickReplies`` row.

    Default behavior (no ``action``): clicking sends ``value`` back to the
    agent (``label`` when ``value`` is absent) — a ``to_assistant`` round-trip.

    Set ``action`` to override the click behavior. With an ``open_url``
    action the pill **redirects** (opens the URL, honoring ``target``)
    instead of messaging the agent — useful for "View your order",
    "Checkout", help links, etc. ``value`` is then optional.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, description="Button text shown to the user.")
    value: Optional[str] = Field(
        None,
        description=(
            "Payload sent to the agent. Falls back to ``label`` when absent — "
            "lets the display text differ from the agent instruction. Ignored "
            "when ``action`` is an ``open_url`` redirect."
        ),
    )
    action: Optional[ActionUnion] = Field(
        None,
        description=(
            "Optional click action. When omitted the pill sends ``value`` to "
            "the agent (default). An ``open_url`` action makes the pill "
            "redirect instead of messaging the agent."
        ),
    )


class QuickReplies(_CatalogBase):
    """Turn-scoped quick-reply row — the LLM-driven equivalent of startup
    chicklets.

    Emit at the end of any assistant turn where you want to suggest 2–5
    follow-up options. The widget renders them as pill buttons right below
    the message; clicking one fires a ``to_assistant`` action and disables
    the whole row (one-shot).

    Use ``QuickReplies`` instead of ``Buttons`` when the options are
    *transient turn suggestions* (e.g. "Do you want to confirm?", "See
    more options", "Back to menu"). Use ``Buttons``/``Button`` when the
    action is a persistent CTA (checkout, product link, handoff).

    Example::

        {"op":"add","id":"qr1","type":"QuickReplies","props":{"items":[
            {"label":"Yes, confirm"},
            {"label":"No, cancel"},
            {"label":"Show me alternatives"}
        ]}}
    """

    items: List[QuickReplyItem] = Field(
        ...,
        # min 1, not 2 (2026-08-03): a single decisive follow-up ("Add BB
        # Bottle to cart") is legitimate — the rider harvest surfaced live
        # single-chip emissions that the old minimum silently discarded.
        min_length=1,
        max_length=8,
        description="1–8 reply options. Shown as pill buttons.",
    )


class LinkButton(_CatalogBase):
    """Single link CTA — the link-only answer ("just give me the checkout
    link") without rendering a whole component around it.

    Literal component (like QuickReplies), but the URL is NOT
    model-trusted: ``execute_render_ui`` rejects any url that is neither
    in the template's ``render_ui.trusted_link_urls`` allowlist nor
    present verbatim in THIS turn's tool results — the model selects
    links, it never authors them. Clicking fires the widget's ``open_url``
    route (Handoff popup semantics, host-page intercept preserved).

    Core, not flavor-owned: the trust rule, the config key and the three
    engine call sites that special-case it are all engine-side, so the
    schema belongs here too. A flavor may still supply its own wording
    for the LLM-facing ``link`` arg through its render_ui pack.

    ``text_channel=False``: the trust check lives ONLY in the render_ui
    function path, so a hand-typed ``<ui_stream>`` add is rejected at
    parse (``render_ui_only:LinkButton``) — the dual-read window must not
    be an arbitrary-URL injection surface."""

    text_channel: ClassVar[bool] = False

    label: str = Field(..., min_length=1, max_length=40)
    url: str = Field(..., min_length=8, max_length=2048, pattern=r"^https://")


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
# Effects primitives (group: effects)
#
# Invisible primitives that run a browser-side side-effect when mounted.
# Distinct from UI primitives — they render no DOM. Used for things the
# server can describe but cannot do itself (set a host-page cookie, ping
# a host-only endpoint, etc.) and are auditable as ops in the SpecStream.
# ---------------------------------------------------------------------------


class SideEffectKind(str, Enum):
    """What a SideEffect op does on mount. Closed set keeps the surface
    small and the side-effects auditable.

    * ``fetch`` — fire-and-forget ``fetch(url, …)``. Same-origin or
      CORS-enabled URLs only. Used to ping host-only endpoints, trigger
      session-bound mutations, or kick off prefetches. Requires ``url``.
    * ``set_cookie`` — write a cookie on the host page via
      ``document.cookie`` (same-origin only — the browser enforces this).
      Used to pin host-page state to server-built identifiers (e.g.
      pointing a storefront's cookie cart at a server-created cart).
      Requires ``name`` and ``value``.
    """

    fetch = "fetch"
    set_cookie = "set_cookie"


class SideEffect(_CatalogBase):
    """Invisible primitive that runs a browser-side side-effect on mount.

    Renders nothing. Used for things the server can describe but cannot do
    itself — e.g. pinning a host-page cookie to a server-built identifier,
    pinging a host-only endpoint, prefetching.

    Field requirements vary by ``kind`` — ``fetch`` uses ``url`` (+ method
    / credentials), ``set_cookie`` uses ``name`` / ``value`` (+ path /
    samesite / secure). Other fields are ignored.

    Idempotency: the widget dedupes by ``(kind, …action-fingerprint…)``
    per page session so re-renders of the same op — or the same value
    emitted across multiple turns / multiple widget instances — fire the
    side-effect at most once. Set ``once=False`` to opt out (e.g. for
    periodic pings).

    Errors are swallowed and logged by the widget so a failed side-effect
    never breaks the conversation flow.
    """

    kind: SideEffectKind = SideEffectKind.fetch
    # --- kind=fetch fields ---
    url: Optional[str] = Field(
        None,
        description=(
            "Target URL (required when ``kind='fetch'``). Same-origin paths"
            " or absolute URLs. Cross-origin requires server CORS."
        ),
    )
    method: Optional[Literal["GET", "POST"]] = "GET"
    credentials: Optional[Literal["omit", "same-origin", "include"]] = "include"
    # --- kind=set_cookie fields ---
    name: Optional[str] = Field(
        None,
        description="Cookie name (required when ``kind='set_cookie'``).",
    )
    value: Optional[str] = Field(
        None,
        description="Cookie value (required when ``kind='set_cookie'``).",
    )
    path: Optional[str] = Field(
        "/",
        description="Cookie path (``kind='set_cookie'``). Default '/'.",
    )
    samesite: Optional[Literal["Strict", "Lax", "None"]] = Field(
        "Lax",
        description="Cookie SameSite (``kind='set_cookie'``).",
    )
    secure: bool = Field(
        True,
        description="Cookie Secure flag (``kind='set_cookie'``). Default True.",
    )
    # --- common ---
    once: bool = Field(
        default=True,
        description=("Dedupe by (kind, fingerprint) per page session. Default true."),
    )

    @model_validator(mode="after")
    def _require_kind_specific_fields(self) -> "SideEffect":
        """Enforce kind-specific required fields.

        The fields are declared ``Optional[...]`` on the model so that one
        Pydantic class can carry both ``fetch`` and ``set_cookie`` shapes,
        but the contract is that ``url`` is required for ``fetch`` and
        ``name`` + ``value`` are required for ``set_cookie``. Without this
        validator, ``validate_props`` would silently accept malformed
        ops (e.g. a ``fetch`` op with no url, or a ``set_cookie`` op with
        no name) and the widget would no-op them — which is graceful at
        runtime but masks template-author bugs. Validate them at parse
        time so the healer rejects them before they reach the widget.
        """
        if self.kind == SideEffectKind.fetch:
            if not self.url:
                raise ValueError(
                    "SideEffect kind='fetch' requires a non-empty 'url' prop"
                )
        elif self.kind == SideEffectKind.set_cookie:
            if not self.name:
                raise ValueError(
                    "SideEffect kind='set_cookie' requires a non-empty 'name' prop"
                )
            if not self.value:
                raise ValueError(
                    "SideEffect kind='set_cookie' requires a non-empty 'value' prop"
                )
        return self


# ---------------------------------------------------------------------------
# Metric primitives (group: metrics)
#
# Single-number / stat widgets for analytics dashboards — the "big number"
# tiles, not full charts. Rendered by the widget like every other primitive.
# Numeric display strings are passed pre-formatted (e.g. "2,999", "₹1,299")
# so locale formatting stays the upstream tool's job.
# ---------------------------------------------------------------------------


class KPI(_CatalogBase):
    """A headline metric: a big value with a label and optional change.
    Use for the key number in an analytics answer (e.g. total calls, success
    rate). ``value`` is a pre-formatted display string."""

    label: str = Field(
        ..., min_length=1, description="What the number measures (e.g. 'Total calls')."
    )
    value: str = Field(
        ...,
        min_length=1,
        description="The metric, pre-formatted (e.g. '2,999', '87%').",
    )
    delta: Optional[str] = Field(
        None, description="Optional change vs a baseline, pre-formatted (e.g. '+12%')."
    )
    trend: Optional[Literal["up", "down", "flat"]] = Field(
        None, description="Direction of the change; colours the delta + arrow."
    )


class MetricCard(_CatalogBase):
    """A metric inside a bordered card: title + big value + optional caption.
    Use for a labelled stat tile in a grid of metrics."""

    title: str = Field(
        ..., min_length=1, description="Card title (e.g. 'No-answer rate')."
    )
    value: str = Field(..., min_length=1, description="The metric, pre-formatted.")
    caption: Optional[str] = Field(
        None, description="Optional supporting text under the value."
    )
    tone: Optional[Literal["neutral", "positive", "warning", "info"]] = Field(
        "neutral", description="Accent colour for the card."
    )


class Sparkline(_CatalogBase):
    """A tiny inline trend line (no axes/labels) for a sequence of numbers.
    Use beside a KPI to show its recent movement compactly."""

    values: List[float] = Field(
        ..., min_length=2, description="Ordered numeric points to plot."
    )
    color: Optional[str] = Field(None, description="Optional stroke colour (CSS).")


class ProgressBar(_CatalogBase):
    """A horizontal bar showing progress toward a goal (value out of max).
    Use for rates / completion (e.g. '87% confirmed')."""

    value: float = Field(
        ..., ge=0, description="Current value (clamped to [0, max] at render)."
    )
    max: float = Field(100, gt=0, description="Maximum value (default 100).")
    label: Optional[str] = Field(
        None, description="Optional label shown above the bar."
    )
    tone: Optional[Literal["neutral", "positive", "warning", "info"]] = Field(
        "neutral", description="Accent colour for the filled portion."
    )


# ---------------------------------------------------------------------------
# Chart primitives (group: graphs)
#
# Rendered by local Svelte chart primitives (BarChart.svelte, LineChart.svelte,
# …) in the Buddy Assist widget — the widget-side render support for these
# schemas, same pattern as the core primitives. All charts share the same tiny
# {label, value} data shape so the LLM can swap chart types freely.
# ---------------------------------------------------------------------------


class ChartDatum(BaseModel):
    """One point in a chart: a category label and its numeric value."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(
        ..., min_length=1, description="Category label shown on the axis or legend."
    )
    value: float = Field(..., description="Numeric value for this point.")
    color: Optional[str] = Field(
        None, description="Optional CSS colour; falls back to the palette by order."
    )


class BarChart(_CatalogBase):
    """Vertical bar chart for comparing discrete categories (e.g. calls per
    hour, outcomes by type, top merchants). Use when comparing counts across
    a small set of labelled categories."""

    data: List[ChartDatum] = Field(
        ..., min_length=1, description="Bars to plot, one per category."
    )
    title: Optional[str] = Field(None, description="Heading shown above the chart.")


class LineChart(_CatalogBase):
    """Line chart for a trend across an ordered sequence (e.g. attempts per day,
    connect rate over a week). Use to show change over an ordered axis."""

    data: List[ChartDatum] = Field(
        ..., min_length=1, description="Points in order, plotted left to right."
    )
    title: Optional[str] = Field(None, description="Heading shown above the chart.")


class AreaChart(_CatalogBase):
    """Filled area chart for volume over time (e.g. daily call or chat volume).
    Like a line chart but emphasises magnitude with a filled region."""

    data: List[ChartDatum] = Field(
        ..., min_length=1, description="Points in order, plotted left to right."
    )
    title: Optional[str] = Field(None, description="Heading shown above the chart.")


class PieChart(_CatalogBase):
    """Pie or donut chart for share-of-total (e.g. outcome breakdown, channel
    split). Use when parts sum to a meaningful whole. Set ``donut`` for a ring
    with a centre total."""

    data: List[ChartDatum] = Field(
        ..., min_length=1, description="Slices; each value is a share of the total."
    )
    title: Optional[str] = Field(None, description="Heading shown above the chart.")
    donut: Optional[bool] = Field(
        None,
        description="Render as a donut ring with a centre total instead of a full pie.",
    )


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
    "QuickReplies": QuickReplies,
    "LinkButton": LinkButton,
    # Data (core)
    "Table": Table,
    # Typed (core)
    "Message": Message,
    "Handoff": Handoff,
    # Composite
    "Tile": Tile,
    # Metrics (analytics stats)
    "KPI": KPI,
    "MetricCard": MetricCard,
    "Sparkline": Sparkline,
    "ProgressBar": ProgressBar,
    # Charts (graphs)
    "BarChart": BarChart,
    "LineChart": LineChart,
    "AreaChart": AreaChart,
    "PieChart": PieChart,
    # Effects (invisible)
    "SideEffect": SideEffect,
    # Lazy flavor groups (e.g. commerce) register here at load time via
    # ``register_primitives`` — see LAZY_GROUPS below.
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
        "QuickReplies",
        "LinkButton",
        "Table",
        "Message",
        "Handoff",
    ],
    "composite": [
        "Tile",
    ],
    "effects": [
        "SideEffect",
    ],
    "graphs": [
        "BarChart",
        "LineChart",
        "AreaChart",
        "PieChart",
    ],
    # Flavor groups (e.g. "commerce" — data-bound, catalog v2 / RFC-001)
    # are NOT listed here: they live under ``assist/<flavor>/schemas.py``
    # and self-register via ``register_primitives`` when a template that
    # enables them is resolved (see LAZY_GROUPS below).
    # Reserved for future expansion — schemas + widget components land
    # behind these group flags so existing templates aren't affected.
    "metrics": [
        "KPI",
        "MetricCard",
        "Sparkline",
        "ProgressBar",
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


# Reverse index: primitive name → owning group. Seeded from the static
# groups above and extended by ``register_primitives``, which uses it to
# reject a name two groups both claim. Keeping it here (rather than
# scanning ``PRIMITIVE_GROUPS`` per lookup) makes ``group_for`` O(1) — it
# runs per rendered node.
_GROUP_OF: Dict[str, str] = {
    name: group for group, members in PRIMITIVE_GROUPS.items() for name in members
}


# Order in which primitives appear in the system-prompt rendering. Listed
# composite-first so the LLM defaults to Tile for list items, then core
# building blocks for freeform composition. Lazily-registered data-bound
# flavor components append at the END — position is irrelevant for them
# (they render in their own "### Data-bound components" prompt subsection;
# only their relative order matters).
PRIMITIVE_RENDER_ORDER: List[str] = [
    # Composite first — preferred for free-form list items
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
    "QuickReplies",
    "LinkButton",
    "Handoff",
    # Data
    "Table",
    # Metrics (analytics stats)
    "KPI",
    "MetricCard",
    "Sparkline",
    "ProgressBar",
    # Charts
    "BarChart",
    "LineChart",
    "AreaChart",
    "PieChart",
    # Notifications
    "Message",
    # Effects (invisible — last so it doesn't crowd the visual catalog)
    "SideEffect",
]


# ---------------------------------------------------------------------------
# Lazy flavor groups
#
# Flavor content (commerce today; future verticals tomorrow) lives under
# ``breeze_buddy/assist/<flavor>/`` and must never load in a process that
# doesn't use it. Each entry maps a group name to the module that, on
# import, self-registers its primitives via ``register_primitives``.
# ``resolve_allowlist`` triggers the load for exactly the lazy groups a
# template enables — it is the single choke point every session path
# (ChatAgent init, prompt splice, parse validation) already runs through,
# and it runs BEFORE ``ui_prompt.render_primitives_section`` computes its
# lru_cache key, so a cached prompt section can never predate the types
# its allowlist names.
# ---------------------------------------------------------------------------


LAZY_GROUPS: Dict[str, str] = {
    "commerce": "app.ai.voice.agents.breeze_buddy.assist.commerce",
}

# Attempted groups — both the successes (``importlib.import_module`` is
# already idempotent via sys.modules; this skips the call on the hot path)
# and the failures, which must NOT be retried per request: Python drops a
# failed module from sys.modules, so an unguarded retry re-raises on every
# call for the life of the process. A broken flavor needs a deploy to fix,
# not another import.
_LOADED_LAZY_GROUPS: Set[str] = set()


def ensure_group_loaded(group: str) -> None:
    """Idempotently import (and thereby register) a lazy flavor group.

    No-op for unknown or already-loaded groups. Registration is
    process-global and additive-only: loading a flavor never mutates or
    removes existing catalog entries, so per-session gating stays purely
    allowlist-driven.

    Fail-open by contract: a flavor module that is absent (its layer has
    not shipped yet) or raises on import must not take down the request.
    The group simply registers nothing, so ``resolve_allowlist`` yields the
    core primitives and the session degrades to v1 — the same outcome as a
    template that never enabled the group — instead of surfacing an
    ImportError as a 500 from whatever endpoint happened to touch it.
    """
    module_path = LAZY_GROUPS.get(group)
    if module_path is None or group in _LOADED_LAZY_GROUPS:
        return
    try:
        importlib.import_module(module_path)
    except Exception:  # noqa: BLE001 — fail-open; a flavor is never load-bearing
        logger.exception(
            f"ui_catalog: flavor group {group!r} failed to load from "
            f"{module_path!r}; its components stay unregistered and sessions "
            "enabling it degrade to the core catalog"
        )
    # Marked either way — see _LOADED_LAZY_GROUPS.
    _LOADED_LAZY_GROUPS.add(group)


def register_primitives(
    group: str,
    primitives: Dict[str, Type[_CatalogBase]],
    *,
    render_order: Optional[List[str]] = None,
) -> None:
    """Register a flavor group's primitives into the process-global catalog.

    Called by a flavor's schema module at import time. Idempotent —
    re-registration overwrites the class mapping (same classes on a
    re-import) without duplicating group-member or render-order entries.
    ``render_order`` defaults to the mapping's insertion order; names
    append at the END of ``PRIMITIVE_RENDER_ORDER`` (see the note there).

    A name already owned by a DIFFERENT group raises. ``UI_CATALOG`` is a
    flat process-global map while the allowlist is per-template, so a
    second group claiming an existing name would silently hand its schema
    to every template that enabled the first — the kind of packaging bug
    that surfaces as one merchant's component validating against
    another's fields. Two groups wanting the same concept means promoting
    it to ``core``, not registering it twice.
    """
    members = PRIMITIVE_GROUPS.setdefault(group, [])
    for name, schema in primitives.items():
        owner = _GROUP_OF.get(name)
        if owner is not None and owner != group:
            raise ValueError(
                f"UI primitive {name!r} is already registered by group "
                f"{owner!r}; group {group!r} cannot claim it"
            )
        UI_CATALOG[name] = schema
        _GROUP_OF[name] = group
        if name not in members:
            members.append(name)
    for name in render_order if render_order is not None else list(primitives):
        if name not in PRIMITIVE_RENDER_ORDER:
            PRIMITIVE_RENDER_ORDER.append(name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def group_for(type_name: str) -> Optional[str]:
    """Return the group name a primitive belongs to, or None if unknown."""
    return _GROUP_OF.get(type_name)


def is_known_type(type_name: str) -> bool:
    """True when ``type_name`` is a registered primitive (regardless of
    whether any template has it enabled)."""
    return type_name in UI_CATALOG


def is_data_bound(type_name: str) -> bool:
    """True when ``type_name`` is a registered data-bound component —
    a `show`-op target hydrated server-side from tool results."""
    schema = UI_CATALOG.get(type_name)
    return bool(schema is not None and schema.data_bound)


def data_bound_names() -> Set[str]:
    """All registered data-bound component names (allowlist-agnostic)."""
    return {name for name, schema in UI_CATALOG.items() if schema.data_bound}


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

    Lazy flavor groups (``LAZY_GROUPS``) are imported-and-registered here,
    exactly when a template enables them — the single trigger point for
    flavor loading. A one-off ``enabled_primitives`` name belonging to a
    lazy group that is NOT in ``enabled_groups`` stays unknown (silently
    ignored, same as any unknown name): flavors are enabled by group.
    """
    if enabled_groups is None and not enabled_primitives:
        enabled_groups = ["core"]
    enabled_groups = enabled_groups or []
    enabled_primitives = enabled_primitives or []
    for group in enabled_groups:
        ensure_group_loaded(group)
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
    "CATALOG_VERSION_V2",
    "UI_CATALOG",
    "PRIMITIVE_GROUPS",
    "PRIMITIVE_RENDER_ORDER",
    "LAZY_GROUPS",
    "ensure_group_loaded",
    "register_primitives",
    "group_for",
    "is_known_type",
    "is_data_bound",
    "data_bound_names",
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
