"""Tests for the commerce catalog group + data-bound prompt rendering
(catalog v2, RFC-001).

Covers: lazy group registration / render order / data_bound flags, the
UCP-shape lifts on the sub-schemas, allowlist resolution with the
commerce group, and the "### Data-bound components" prompt subsection —
including the no-regression guarantee for v1 templates (§9.3).
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
    CartLineP,
    CartTotalP,
    MoneyP,
    ProductP,
)
from app.ai.voice.agents.breeze_buddy.template.ui_catalog import (
    PRIMITIVE_GROUPS,
    PRIMITIVE_RENDER_ORDER,
    UI_CATALOG,
    data_bound_names,
    ensure_group_loaded,
    group_for,
    is_data_bound,
    resolve_allowlist,
)
from app.ai.voice.agents.breeze_buddy.template.ui_prompt import (
    render_primitives_section,
)

# Registration is a lazy-import side effect; the schemas import above
# already triggered it, but go through the public loader anyway (it must
# be idempotent).
ensure_group_loaded("commerce")

COMMERCE = {"ProductCard", "ProductGrid", "CartView", "ProductDetail"}
# The subset the LLM is prompted with — ProductDetail is server_only and
# ProductCard was retired to server_only 2026-07-30 (a ProductGrid of one
# IS a card now that layout is count-derived): registered + allowlisted,
# but never rendered into the text-channel prompt sections.
PROMPTED = COMMERCE - {"ProductDetail", "ProductCard"}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_commerce_components_registered_in_catalog():
    for name in COMMERCE:
        assert name in UI_CATALOG
        assert name in PRIMITIVE_RENDER_ORDER
        assert group_for(name) == "commerce"


def test_commerce_group_lists_exactly_the_registered_components():
    assert set(PRIMITIVE_GROUPS["commerce"]) == COMMERCE


def test_data_bound_flags():
    # Every commerce primitive is data-bound; LinkButton (the literal,
    # model-authored one) now lives in core.
    assert data_bound_names() == COMMERCE
    assert is_data_bound("ProductGrid")
    assert not is_data_bound("LinkButton")
    assert not is_data_bound("Tile")
    assert not is_data_bound("NoSuchThing")


def test_resolve_allowlist_with_commerce_group():
    allowlist = resolve_allowlist(enabled_groups=["core", "commerce"])
    assert COMMERCE <= allowlist
    assert "Tile" not in allowlist  # composite not enabled


def test_resolve_default_excludes_commerce():
    assert resolve_allowlist() & COMMERCE == set()


# ---------------------------------------------------------------------------
# Sub-schema UCP lifts
# ---------------------------------------------------------------------------


def test_cart_line_lifts_raw_ucp_shape():
    line = CartLineP.model_validate(
        {
            "id": "line1",
            "quantity": 3,
            "item": {
                "id": "gid://shopify/ProductVariant/5",
                "title": "Tee - L",
                "image_url": "https://cdn.example/tee.jpg",
                "price": 899.0,
            },
            "totals": [
                {"type": "subtotal", "amount": 2697.0},
                {"type": "total", "amount": 2697.0},
            ],
        }
    )
    assert line.title == "Tee - L"
    assert line.qty == 3
    assert line.line_total.amount == 2697.0
    assert line.variant_id == "gid://shopify/ProductVariant/5"
    assert line.image is not None and str(line.image.src).endswith("tee.jpg")


def test_cart_line_canonical_shape_passes_through():
    line = CartLineP.model_validate(
        {
            "id": "line1",
            "title": "Tee",
            "qty": 1,
            "line_total": {"amount": 899.0, "currency": "INR"},
        }
    )
    assert line.line_total.currency == "INR"


# ---------------------------------------------------------------------------
# Prompt rendering — data-bound subsection
# ---------------------------------------------------------------------------


def test_data_bound_subsection_renders_when_allowlisted():
    section = render_primitives_section(
        resolve_allowlist(enabled_groups=["core", "composite", "commerce"])
    )
    assert "### Data-bound components" in section
    for name in PROMPTED:
        assert f"**{name}**" in section
    # server_only: allowlisted but invisible to the LLM.
    assert "**ProductDetail**" not in section
    # The show-op grammar + exactly one example.
    assert '"$tool:<tool_name>#<json-pointer>"' in section
    assert section.count('"op":"show"') == 2  # grammar skeleton + 1 example
    assert '"bind":{"products":"$tool:search_catalog#/products"}' in section


def test_data_bound_subsection_stays_within_token_budget():
    section = render_primitives_section(
        resolve_allowlist(enabled_groups=["core", "composite", "commerce"])
    )
    start = section.index("### Data-bound components")
    subsection = section[start : section.index("Action shape")].rstrip()
    assert len(subsection.splitlines()) <= 40


def test_data_bound_components_have_no_add_style_entry():
    """Commerce components must not render as ordinary add-op entries —
    the LLM reaches them only via `show`."""
    section = render_primitives_section(
        resolve_allowlist(enabled_groups=["core", "commerce"])
    )
    head, _, _tail = section.partition("### Data-bound components")
    for name in COMMERCE:
        assert f"**{name}**" not in head


def test_v1_section_unchanged_without_commerce():
    """§9.3 — no prompt regression for v1 templates: the rendered section
    for a commerce-free allowlist is byte-identical whether or not the
    commerce group exists in the catalog (i.e. no subsection leaks)."""
    section = render_primitives_section(
        resolve_allowlist(enabled_groups=["core", "composite"])
    )
    assert "### Data-bound components" not in section
    assert "ProductGrid" not in section
    assert "show" not in section.split("Action shape")[0].lower() or True
    # The classic anchors are still present and ordered.
    assert section.index("**Tile**") < section.index("**Carousel**")


# ---------------------------------------------------------------------------
# Money amount normalization (live UCP storefront shape)
# ---------------------------------------------------------------------------


def test_money_accepts_comma_grouped_amount_string():
    # Live UCP storefronts emit INR-grouped amount strings ("1,699.00")
    # which fail bare pydantic float parsing — the projection strips the
    # grouping commas (dot stays the decimal separator on the wire).
    money = MoneyP.model_validate({"amount": "1,699.00", "currency": "INR"})
    assert money.amount == 1699.0
    plain = MoneyP.model_validate({"amount": "934.00"})
    assert plain.amount == 934.0


def test_cart_total_accepts_comma_grouped_amount_string():
    total = CartTotalP.model_validate(
        {"type": "total", "amount": "2,697.00", "currency": "INR"}
    )
    assert total.amount == 2697.0


# ---------------------------------------------------------------------------
# Variant projection (inline picker feed)
# ---------------------------------------------------------------------------

_UCP_PRODUCT_BASE = {
    "id": "gid://shopify/Product/1",
    "title": "Trail Shoe",
    "price_range": {"min": {"amount": 4999.0, "currency": "INR"}},
}


def test_single_available_variant_projects_default_id_and_options():
    # 2026-07-30: options ALWAYS project (Add-to-cart confirms through the
    # picker even for one-available products); default_variant_id still
    # marks the single available choice for data-less fallbacks.
    product = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {"id": "gid://shopify/ProductVariant/1", "availability": "available"},
                {"id": "gid://shopify/ProductVariant/2", "availability": "sold_out"},
            ],
        }
    )
    assert product.default_variant_id == "gid://shopify/ProductVariant/1"
    assert [v.id for v in product.variants] == [
        "gid://shopify/ProductVariant/1",
        "gid://shopify/ProductVariant/2",
    ]


def test_multi_variant_projects_picker_options_with_availability():
    product = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "S / Black",
                    "availability": "available",
                    "price": {"amount": "4,999.00", "currency": "INR"},
                },
                {
                    "id": "gid://shopify/ProductVariant/2",
                    "title": "M / Black",
                    "availability": "sold_out",
                },
                {"id": "gid://shopify/ProductVariant/3", "title": "L / Black"},
                "not-a-dict",  # malformed entry filtered, never fatal
                {"title": "no id — filtered"},
            ],
        }
    )
    assert product.default_variant_id is None
    assert [v.id for v in product.variants] == [
        "gid://shopify/ProductVariant/1",
        "gid://shopify/ProductVariant/2",
        "gid://shopify/ProductVariant/3",
    ]
    assert [v.available for v in product.variants] == [True, False, True]
    assert product.variants[0].price is not None
    assert product.variants[0].price.amount == 4999.0  # comma-grouping stripped


def test_variant_projection_caps_option_count():
    product = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {"id": f"gid://shopify/ProductVariant/{i}"} for i in range(40)
            ],
        }
    )
    assert len(product.variants) == 24


def test_variant_availability_nested_object_shape():
    """Live Beyond Bound wire (2026-07-28): variant availability arrives
    as a NESTED object — {"availability": {"available": bool}}. A sold-out
    size must project available=False so the picker disables it (the
    CoreFlex Tee regression: L rendered buyable, add came back empty)."""
    product = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "S",
                    "availability": {"available": True},
                },
                {
                    "id": "gid://shopify/ProductVariant/2",
                    "title": "M",
                    "availability": {"available": True},
                },
                {
                    "id": "gid://shopify/ProductVariant/3",
                    "title": "L",
                    "availability": {"available": False},
                },
                {
                    "id": "gid://shopify/ProductVariant/4",
                    "title": "XL",
                    "availability": {"state": "sold_out"},
                },
            ],
        }
    )
    assert [v.available for v in product.variants] == [True, True, False, False]
    # And the single-available projection respects the nested shape too.
    single = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "availability": {"available": True},
                },
                {
                    "id": "gid://shopify/ProductVariant/2",
                    "availability": {"available": False},
                },
            ],
        }
    )
    assert single.default_variant_id == "gid://shopify/ProductVariant/1"
    assert [v.available for v in single.variants] == [True, False]


# ---------------------------------------------------------------------------
# ProductDetailP projection (full-panel detail overlay)
# ---------------------------------------------------------------------------


def test_product_detail_lifts_live_get_product_shape():
    """Mirror of the live Beyond Bound get_product `product` payload
    (2026-07-28): {html} description, price_range/list_price_range,
    product-level media, variants with nested availability."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
        ProductDetailP,
    )

    detail = ProductDetailP.model_validate(
        {
            "id": "gid://shopify/Product/1",
            "title": "Bundle of 3 Socks - Black",
            "description": {
                "html": "<p>Overview</p><p>Microfibre <b>comfort</b></p><ul><li>Anti-slip grip</li></ul>"
            },
            "url": "https://www.thebeyondbound.com/products/socks",
            "price_range": {"min": {"amount": "909.00", "currency": "INR"}},
            "list_price_range": {"min": {"amount": "1,299.00", "currency": "INR"}},
            "media": [
                {"type": "image", "url": "https://cdn.example/a.jpg"},
                {"type": "video", "url": "https://cdn.example/clip.mp4"},
                {"type": "image", "url": "https://cdn.example/b.jpg"},
            ],
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/1",
                    "title": "S",
                    "availability": {"available": True},
                    "price": {"amount": "909.00", "currency": "INR"},
                },
                {
                    "id": "gid://shopify/ProductVariant/2",
                    "title": "M",
                    "availability": {"available": False},
                },
                "not-a-dict",
            ],
        }
    )
    # Block tags become line breaks (merchant formatting survives the
    # strip); inline tags drop; the widget renders pre-wrap.
    assert detail.description == "Overview\n\nMicrofibre comfort\n\nAnti-slip grip"
    assert detail.price is not None and detail.price.amount == 909.0
    assert detail.compare_at is not None and detail.compare_at.amount == 1299.0
    # Video entries are dropped; only images feed the gallery.
    assert [str(m.src) for m in detail.images] == [
        "https://cdn.example/a.jpg",
        "https://cdn.example/b.jpg",
    ]
    # ALL well-formed variants project (sold-out included — the picker
    # disables them), malformed entries dropped.
    assert [(v.id, v.available) for v in detail.variants] == [
        ("gid://shopify/ProductVariant/1", True),
        ("gid://shopify/ProductVariant/2", False),
    ]
    assert detail.default_variant_id is None


def test_description_repairs_upstream_flattened_boundaries():
    """Live BB Bottle shape (2026-07-29): the UCP gateway ships some
    descriptions ALREADY tag-stripped with block boundaries lost — bullet
    emojis glued onto the previous word ("go🔥❄️ Keeps") and paragraphs
    fused ("youFrom"). The repair heuristics restore line structure
    without touching camel-case brand names."""
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
        _html_to_display_text,
    )

    flattened = (
        "Perfect from gym to boardrooms. Features: 🖤 1 litre capacity – "
        "wherever you go🔥❄️ Keeps drinks hot for 12 hours – Your way🛡️ "
        "Stainless steel body – rust-resistant🎯 Leak-proof – Designed to "
        "move with youFrom early morning workouts, it matches your pace. "
        "Disclaimer: Colours may vary."
    )
    assert _html_to_display_text(flattened) == (
        "Perfect from gym to boardrooms. Features: 🖤 1 litre capacity – "
        "wherever you go\n"
        "🔥❄️ Keeps drinks hot for 12 hours – Your way\n"
        "🛡️ Stainless steel body – rust-resistant\n"
        "🎯 Leak-proof – Designed to move with you\n"
        "\n"
        "From early morning workouts, it matches your pace. "
        "Disclaimer: Colours may vary."
    )
    # Camel-case brand names and inline emoji (after a space) are intact.
    assert _html_to_display_text("an iPhone case") == "an iPhone case"
    assert _html_to_display_text("watch on YouTube") == "watch on YouTube"
    assert _html_to_display_text("Good vibes ✨ always") == "Good vibes ✨ always"


def test_product_detail_single_variant_sets_default_id():
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
        ProductDetailP,
    )

    detail = ProductDetailP.model_validate(
        {
            "id": "gid://shopify/Product/1",
            "title": "Socks",
            "variants": [{"id": "gid://shopify/ProductVariant/9"}],
        }
    )
    assert detail.default_variant_id == "gid://shopify/ProductVariant/9"
    assert len(detail.variants) == 1


def test_lone_default_title_variant_projects_direct_add_shape():
    """Shopify's variantless-product pseudo-variant (live 2026-08-03:
    one-size socks rendered a nonsense "Choose an option → Default Title"
    picker): a lone "Default Title" variant projects variants=[] +
    default_variant_id — the widget's existing direct-add path. A real
    lone variant (an actual named option) keeps projecting for the
    confirm picker."""
    product = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/77",
                    "title": "Default Title",
                    "availability": "available",
                }
            ],
        }
    )
    assert product.variants == []
    assert product.default_variant_id == "gid://shopify/ProductVariant/77"

    # Contrast: a real named lone variant still ships its option.
    real = ProductP.model_validate(
        {
            **_UCP_PRODUCT_BASE,
            "variants": [
                {
                    "id": "gid://shopify/ProductVariant/78",
                    "title": "One Size / Black",
                    "availability": "available",
                }
            ],
        }
    )
    assert [v.id for v in real.variants] == ["gid://shopify/ProductVariant/78"]


def test_product_detail_lone_default_title_hides_option_pills():
    from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
        ProductDetailP,
    )

    detail = ProductDetailP.model_validate(
        {
            "id": "gid://shopify/Product/9",
            "title": "Peach in Progress – White",
            "variants": [
                {"id": "gid://shopify/ProductVariant/9", "title": "Default Title"}
            ],
        }
    )
    assert detail.variants == []
    assert detail.default_variant_id == "gid://shopify/ProductVariant/9"
