"""Commerce component schemas (group: commerce) — Component Catalog v2 (RFC-001).

Data-bound: the LLM emits a `show` op naming the component + bindings into
THIS turn's post-pipeline tool results; the server pointer-walks, validates
against these schemas, and emits an ordinary hydrated `add` op (`v:2`).
Prices / titles / URLs are tool-sourced by construction — the LLM cannot
inject values into bound props.

Sub-schemas are projections of the UCP tool shapes. They deliberately relax
`_CatalogBase`'s extra='forbid' to extra='ignore' (projection semantics —
tool payloads carry fields the component doesn't render) and carry a small
mode='before' lift so the RAW UCP shape (`price_range.min`, `media[0].url`,
cart lines' `item.title` / `quantity`) validates without requiring every
template to add a response projection first. Hydration itself stays a pure
pointer walk (see chat/ui_binding.py).

Lazy-loaded flavor module: importing it registers the components into the
flavor-agnostic catalog (see ``register_primitives`` at the bottom). The
import happens only via ``ui_catalog.ensure_group_loaded("commerce")`` —
i.e. only in processes that resolve a commerce-enabled template.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.hooks import (
    normalize_variants,
    repair_description,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.render_ui import (
    register_commerce_render_ui_pack,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.step_labels import (
    register_commerce_step_labels,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.tool_meta import (
    register_commerce_tool_meta,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.ui_prompt import (
    register_commerce_render_ui_prompt,
)

# Private-name import is deliberate (same posture as ui_binding's
# session_state imports): _CatalogBase is the catalog's schema contract
# (extra='forbid' + the data_bound ClassVar) and re-declaring it here
# would drift.
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    _CatalogBase,
    register_primitives,
)


def _strip_amount_grouping(v: Any) -> Any:
    """Normalize a {amount: "1,699.00"} dict — live UCP storefronts emit
    comma-GROUPED amount strings (INR lakh/thousand grouping) that fail
    pydantic float parsing. Commas are always grouping on the UCP wire
    (dot is the decimal separator), so stripping them is lossless."""
    if isinstance(v, dict):
        amount = v.get("amount")
        if isinstance(amount, str) and "," in amount:
            out = dict(v)
            out["amount"] = amount.replace(",", "")
            return out
    return v


class MoneyP(BaseModel):
    """Money projection — rendered client-side (e.g. "₹1,699") from
    ``currency``. Accepts a bare number as shorthand for {amount}."""

    model_config = ConfigDict(extra="ignore")

    amount: float
    currency: str = "INR"

    @model_validator(mode="before")
    @classmethod
    def _lift_bare_number(cls, v: Any) -> Any:
        if isinstance(v, (int, float)):
            return {"amount": v}
        return _strip_amount_grouping(v)


class MediaP(BaseModel):
    """Image projection: {src, alt}. Accepts the UCP media entry shape
    ({url, alt_text?}) via the lift below."""

    model_config = ConfigDict(extra="ignore")

    src: HttpUrl
    alt: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _lift_ucp_media(cls, v: Any) -> Any:
        if isinstance(v, dict) and "src" not in v and "url" in v:
            out = dict(v)
            out["src"] = out.pop("url")
            if "alt" not in out and out.get("alt_text"):
                out["alt"] = out.pop("alt_text")
            return out
        return v


_UNAVAILABLE_STATES = ("unavailable", "out_of_stock", "sold_out")


def _variant_available(variant: Dict[str, Any]) -> bool:
    """Best-effort availability read off a UCP variant entry. Absent field
    counts as available (conservative — the storefront enforces stock).

    Live Beyond Bound wire shape (2026-07-28): availability is a NESTED
    object — ``{"availability": {"available": bool}}`` — alongside the
    older bool / state-string encodings. All three are handled; anything
    unrecognized stays available (the deterministic cart verifier is the
    backstop for a variant that turns out unbuyable)."""
    availability = variant.get("availability", variant.get("available"))
    if availability is None:
        return True
    if isinstance(availability, bool):
        return availability
    if isinstance(availability, dict):
        inner = availability.get("available")
        if isinstance(inner, bool):
            return inner
        state = availability.get("state") or availability.get("status")
        if isinstance(state, str):
            return state.lower() not in _UNAVAILABLE_STATES
        return True
    if isinstance(availability, str):
        return availability.lower() not in _UNAVAILABLE_STATES
    return True


class VariantP(BaseModel):
    """One purchasable variant option — projection of the UCP product
    ``variants[]`` entry. Feeds the widget's inline variant picker
    (multi-variant add-to-cart happens client-side, deterministically —
    no agent turn)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: Optional[str] = None  # e.g. "S / Black"
    available: bool = True
    price: Optional[MoneyP] = None

    @model_validator(mode="before")
    @classmethod
    def _lift_ucp_variant(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        out = dict(v)
        if "available" not in out:
            out["available"] = _variant_available(out)
        return out


# Hard cap on projected variants — bounds the hydrated payload; a picker
# past this many options is the storefront page's job (View still works).
_MAX_PROJECTED_VARIANTS = 24


class ProductP(BaseModel):
    """One product as rendered by ProductCard / ProductGrid. A projection of
    the UCP catalog product (search_catalog / lookup_catalog entries)."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    url: Optional[str] = None
    image: Optional[MediaP] = None  # {src, alt}
    price: MoneyP
    list_price: Optional[MoneyP] = None
    tags: List[str] = Field(default_factory=list)  # ≤3 rendered
    availability: Optional[str] = None
    variant_axes: Optional[str] = None  # e.g. "S / M / L · 3 colours"
    # Enables the built-in add_to_cart intent on single-variant products
    # (widget renders the secondary action only when present).
    default_variant_id: Optional[str] = None
    # Multi-variant products: the widget's inline picker options. Empty for
    # single-variant products (default_variant_id covers those).
    variants: List[VariantP] = Field(default_factory=list)
    # Variant-continuity anchor (RFC-003 §4): set by the selection stage when
    # a render_ui items[] entry featured a variant — the card's hero fields
    # were re-derived from that variant record, the picker preselects it,
    # and view_product/add_to_cart carry it forward.
    featured_variant_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _lift_ucp_product(cls, v: Any) -> Any:
        """Accept the raw UCP catalog shape: ``price_range.min`` → price,
        ``list_price_range.min`` → list_price, ``media[0]`` → image, and
        project ``variants[]``: ALL well-formed entries ship (capped) —
        the widget's Add-to-cart always confirms through the variant
        picker (2026-07-30 UX), so even a single-available product needs
        its option data (one-chip confirm). Exactly ONE available variant
        additionally sets ``default_variant_id`` (the direct-add fallback
        for payloads with no variant data at all). No-ops when the
        canonical keys are already present."""
        if not isinstance(v, dict):
            return v
        out = dict(v)
        if "price" not in out and isinstance(out.get("price_range"), dict):
            out["price"] = out["price_range"].get("min")
        if "list_price" not in out and isinstance(out.get("list_price_range"), dict):
            out["list_price"] = out["list_price_range"].get("min")
        media = out.get("media")
        if "image" not in out and isinstance(media, list) and media:
            out["image"] = media[0]
        variants = out.get("variants")
        if isinstance(variants, list):
            well_formed = [
                va
                for va in variants
                if isinstance(va, dict) and isinstance(va.get("id"), str)
            ]
            available = [va for va in well_formed if _variant_available(va)]
            if "default_variant_id" not in out and len(available) == 1:
                out["default_variant_id"] = available[0]["id"]
            # ALL well-formed entries project (malformed ones are filtered,
            # never failing the whole product; the cap bounds the payload)
            # — the picker is the confirm step for every add, so even a
            # one-available product ships its options. EXCEPT the lone
            # "Default Title" pseudo-variant: not a real choice, so it
            # never reaches the picker (a connector may suppress it).
            # Use the normalizer's OUTPUT, not just its truthiness — a
            # connector may filter the list rather than clear it, and the
            # card's picker must agree with the detail overlay's (which
            # projects the same call below).
            out["variants"] = normalize_variants(well_formed)[:_MAX_PROJECTED_VARIANTS]
        return out


class CartLineP(BaseModel):
    """One cart line as rendered by CartView. A projection of the UCP cart
    ``line_items[]`` entry."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    variant_title: Optional[str] = None
    image: Optional[MediaP] = None
    qty: int
    line_total: MoneyP
    variant_id: Optional[str] = None  # enables set_qty / remove_line intents

    @model_validator(mode="before")
    @classmethod
    def _lift_ucp_cart_line(cls, v: Any) -> Any:
        """Accept the raw UCP cart line: ``item.title`` → title,
        ``item.image_url`` → image, ``quantity`` → qty, the ``totals[]``
        entry with type=='total' → line_total, ``item.id`` → variant_id."""
        if not isinstance(v, dict):
            return v
        out = dict(v)
        item = out.get("item") if isinstance(out.get("item"), dict) else {}
        if "title" not in out and item.get("title"):
            out["title"] = item["title"]
        if "variant_title" not in out and item.get("variant_title"):
            out["variant_title"] = item["variant_title"]
        if "image" not in out and item.get("image_url"):
            out["image"] = {"src": item["image_url"], "alt": out.get("title")}
        if "qty" not in out and "quantity" in out:
            out["qty"] = out["quantity"]
        if "line_total" not in out:
            totals = out.get("totals")
            if isinstance(totals, list):
                total = next(
                    (
                        t
                        for t in totals
                        if isinstance(t, dict) and t.get("type") == "total"
                    ),
                    None,
                )
                if total is not None:
                    out["line_total"] = total
        if "variant_id" not in out and item.get("id"):
            out["variant_id"] = item["id"]
        return out


class CartTotalP(BaseModel):
    """One row of the cart's ``totals[]`` — matches the UCP shape directly."""

    model_config = ConfigDict(extra="ignore")

    type: str
    amount: float
    currency: str = "INR"

    @model_validator(mode="before")
    @classmethod
    def _normalize_amount(cls, v: Any) -> Any:
        return _strip_amount_grouping(v)


class CheckoutP(BaseModel):
    """Checkout handoff rendered inside CartView (label + URL). The widget
    owns the popup/open semantics — no Handoff op needed."""

    model_config = ConfigDict(extra="ignore")

    label: str = "Checkout"
    url: HttpUrl


class ProductCard(_CatalogBase):
    """Data-bound single-product card — bind ``product`` to one tool-result product.

    The widget renders media/title/price and the built-in ``view_product``
    (+ ``add_to_cart`` when single-variant) intents.

    RETIRED from the LLM surface 2026-07-30 (``server_only``): with layout
    server-derived from the hydrated count, a ProductGrid of one IS a card
    (full-width via the widget's :only-child rule) — the card-vs-grid
    presentation choice stops being the model's. The component stays in the
    catalog for replayed persisted ops and as the grid's building block."""

    data_bound: ClassVar[bool] = True
    server_only: ClassVar[bool] = True

    product: ProductP
    density: Optional[Literal["default", "spacious"]] = "default"


class GridItemSelector(BaseModel):
    """One ``items[]`` selection entry (RFC-003 §4 — the model authors
    SELECTORS, never values). ``feature_variant`` names a variant id from
    that product's tool-sourced variants; the selection stage re-derives the
    card hero (image/price) from that variant record and stamps
    ``featured_variant_id`` for downstream continuity."""

    model_config = ConfigDict(extra="forbid")

    id: str
    feature_variant: Optional[str] = None


class ProductGrid(_CatalogBase):
    """Data-bound product list — bind ``products`` to a tool-result product array.

    Server caps it at ``max_items`` (default 10 = one full UCP search page,
    so the rendered grid matches the LLM's context exactly); each
    card carries the same built-in intents as ProductCard."""

    data_bound: ClassVar[bool] = True
    # ``items`` is the selection DIRECTIVE (applied then stripped server-side
    # in _select_list_props / resolve_show_op) — never a render prop.
    selection_field: ClassVar[Optional[str]] = "items"

    products: List[ProductP] = Field(..., min_length=1, max_length=12)
    max_items: int = Field(10, ge=1, le=12)
    layout: Optional[Literal["grid", "carousel"]] = "grid"
    items: Optional[List[GridItemSelector]] = Field(
        None,
        max_length=12,
        description=(
            "Optional selection: render ONLY the bound products whose id is "
            "listed here, in THIS order (ids from this turn's tool result; "
            "unknown ids are ignored, all-unknown renders the full list). "
            "feature_variant additionally makes that variant the card hero."
        ),
    )


class CartView(_CatalogBase):
    """Data-bound cart render — bind cart fields off the cart tool result.

    Renders header, line rows (with ``remove_line`` / ``set_qty`` intents),
    totals, the checkout handoff button, and the cart-cookie side-effect
    (widget-internal, driven by ``cart_token``) — replacing the
    hand-composed 5-op cart litany."""

    data_bound: ClassVar[bool] = True

    cart_id: str
    line_items: List[CartLineP] = Field(default_factory=list)
    totals: List[CartTotalP] = Field(default_factory=list)
    checkout: Optional[CheckoutP] = None
    cart_token: Optional[str] = None


_TAG_RE = re.compile(r"<[^>]+>")
# Block-level boundaries become line breaks so merchant formatting
# (headings, paragraphs, list items) survives the tag strip — the widget
# renders the description `pre-wrap`.
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ul|ol|h[1-6]|tr|table|blockquote)[^>]*>",
    re.IGNORECASE,
)


def _html_to_display_text(html: str) -> Optional[str]:
    """Merchant description HTML → display-grade plain text: block tags
    become newlines, remaining tags drop, upstream-flattened boundaries
    are repaired, per-line whitespace collapses, blank runs shrink to one
    empty line, capped."""
    text = _TAG_RE.sub(" ", _BLOCK_TAG_RE.sub("\n", html))
    # Gateways that ship descriptions ALREADY tag-stripped leave nothing
    # for the conversion above to work with; a connector may know how to
    # recover the lost boundaries.
    text = repair_description(text)
    lines = [" ".join(line.split()) for line in text.split("\n")]
    collapsed: List[str] = []
    for line in lines:
        if line:
            collapsed.append(line)
        elif collapsed and collapsed[-1] != "":
            collapsed.append("")
    out = "\n".join(collapsed).strip()[:_MAX_DETAIL_DESCRIPTION]
    return out or None


# Bound the hydrated detail payload: description prose and gallery images.
_MAX_DETAIL_DESCRIPTION = 1500
_MAX_DETAIL_IMAGES = 8


class ProductDetailP(BaseModel):
    """Server projection of ONE full product for the detail overlay
    (full-panel PDP the widget opens on View). Lifts the UCP
    ``get_product`` payload's ``product`` object: description arrives as
    ``{"html": …}`` (tags stripped to plain text here — the widget never
    renders merchant HTML), price is ``price_range.min``, compare-at is
    ``list_price_range.min``, gallery is the product-level ``media``.
    Unlike ProductP, ALL well-formed variants project (capped) — the
    picker in the detail view is the primary buy path."""

    model_config = ConfigDict(extra="ignore")

    id: str
    title: str
    description: Optional[str] = None
    url: Optional[HttpUrl] = None
    price: Optional[MoneyP] = None
    compare_at: Optional[MoneyP] = None
    images: List[MediaP] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    variants: List[VariantP] = Field(default_factory=list)
    default_variant_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _lift_ucp_detail(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return v
        out = dict(v)
        desc = out.get("description")
        if isinstance(desc, dict):
            html = desc.get("html")
            out["description"] = (
                _html_to_display_text(html) if isinstance(html, str) else None
            )
        if "price" not in out and isinstance(out.get("price_range"), dict):
            out["price"] = out["price_range"].get("min")
        if "compare_at" not in out and isinstance(out.get("list_price_range"), dict):
            out["compare_at"] = out["list_price_range"].get("min")
        if "images" not in out and isinstance(out.get("media"), list):
            out["images"] = [
                m
                for m in out["media"]
                if isinstance(m, dict)
                and m.get("url")
                and m.get("type", "image") == "image"
            ][:_MAX_DETAIL_IMAGES]
        raw_variants = out.get("variants")
        if isinstance(raw_variants, list):
            well_formed = [
                entry
                for entry in raw_variants
                if isinstance(entry, dict) and entry.get("id")
            ][:_MAX_PROJECTED_VARIANTS]
            if "default_variant_id" not in out and len(well_formed) == 1:
                out["default_variant_id"] = well_formed[0]["id"]
            # A connector may suppress a platform-manufactured
            # pseudo-variant here (an optionless product's lone fake
            # choice): default_variant_id is set above, and the overlay's
            # option pills must not render a choice that isn't one.
            out["variants"] = normalize_variants(well_formed)
        return out


class ProductDetail(_CatalogBase):
    """Server-only full product view — hydrated by the `view_product`
    DIRECT intent into the widget's full-panel overlay. Never offered to
    the LLM (``server_only``): the prompt renders no entry for it, so the
    model cannot emit it; the allowlist still validates it so the server
    path hydrates through the same show-op resolver as everything else."""

    data_bound: ClassVar[bool] = True
    server_only: ClassVar[bool] = True

    product: ProductDetailP


# ---------------------------------------------------------------------------
# Registration — runs once, at (lazy) import time. Templates opt in via
# ``ui_catalog.enabled_groups += ["commerce"]``; the LLM drives these with
# `show` ops, never hand-typed `add` props. Render order matches the old
# in-catalog placement (ProductGrid → ProductCard → CartView) — position in
# the global order is irrelevant for data-bound components (they render in
# their own prompt subsection), only their relative order matters.
# ---------------------------------------------------------------------------

register_primitives(
    "commerce",
    {
        "ProductGrid": ProductGrid,
        "ProductCard": ProductCard,
        "CartView": CartView,
        "ProductDetail": ProductDetail,
    },
)

# Step-progress labels ride the same lazy hook: any process that resolves a
# commerce-enabled template gets the UCP tool → step-line labels alongside
# the component registration (see assist/commerce/step_labels.py).
register_commerce_step_labels()

# Tool safety metadata (annotations + deterministic verifiers) rides the
# same hook — see assist/commerce/tool_meta.py.
register_commerce_tool_meta()

# The render_ui prompt vocabulary rides the same hook — the engine keeps
# only the flavor-neutral contract; the shopper/product/cart copy is
# this flavor's (see assist/commerce/ui_prompt.py).
register_commerce_render_ui_prompt()

# The render_ui SURFACE pack too: schema arg vocabulary, the commerce
# prop-shape summarizer, and post-hydration projection policy (layout,
# CartView checkout stamping) — see assist/commerce/render_ui.py.
register_commerce_render_ui_pack()

# On adding a SECOND flavor: these register_* calls are the whole
# registration surface, and every one now takes the flavor's group as its
# first argument, so a second flavor is additive — it registers under its
# own group and the two never see each other's vocabulary (chat/flavors.py).
#
# Collapsing them into one declarative FlavorManifest has been considered
# and deliberately NOT done: it buys no behaviour, and a manifest that must
# apply transactionally (half a flavor registered is worse than none) is
# more machinery than five call sites justify. Revisit only if flavor #3
# makes the boilerplate a real cost.


__all__ = [
    "MoneyP",
    "MediaP",
    "VariantP",
    "ProductP",
    "CartLineP",
    "CartTotalP",
    "CheckoutP",
    "ProductCard",
    "ProductGrid",
    "CartView",
    "ProductDetailP",
    "ProductDetail",
]
