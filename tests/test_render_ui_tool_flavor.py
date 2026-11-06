"""render_ui flavor-pack registry (engine/flavor segregation).

The engine owns the render_ui mechanism; vocabulary, summaries, and
post-hydration projection policy belong to flavor packs registered under
catalog group names. These tests pin the seam: the generic fallbacks leak
no flavor vocabulary, and a registered pack's text + summarizer win for
sessions whose template enables that group.
"""

from __future__ import annotations

from app.ai.voice.agents.breeze_buddy.chat.ui import render_ui_tool as rut

_COMMERCE_WORDS = ("shopper", "product", "cart", "checkout", "search_catalog")


def test_generic_schema_text_is_flavor_neutral():
    schema = rut.build_render_ui_schema(
        ["QuickReplies", "LinkButton"],
        handler=None,
        trusted_urls=None,
        quick_replies_mode="forced_final",
        flavor_groups=["unregistered_group"],
    )
    blob = (schema.description + repr(schema.properties)).lower()
    for word in _COMMERCE_WORDS:
        assert word not in blob, f"generic render_ui schema leaks {word!r}"


def test_registered_pack_supplies_text_and_summary():
    pack = rut.RenderUiFlavorPack(
        tool_description="Testflavor tool description.",
        items_description="Testflavor selection.",
        summarize=lambda component, props: (
            {"status": "ok", "rendered": component, "flavored": True}
            if component == "TestGrid"
            else None
        ),
    )
    rut.register_render_ui_flavor_pack("testflavor", pack)
    try:
        schema = rut.build_render_ui_schema(
            ["QuickReplies"], handler=None, flavor_groups=["core", "testflavor"]
        )
        assert schema.description == "Testflavor tool description."
        assert schema.properties["items"]["description"] == "Testflavor selection."
        # Pack summarizer wins for its shapes...
        flavored = rut.summarize_render("TestGrid", {"props": {}}, ["testflavor"])
        assert flavored == {"status": "ok", "rendered": "TestGrid", "flavored": True}
        # ...and defers (None) to the generic fallback for engine components.
        generic = rut.summarize_render(
            "QuickReplies", {"props": {"items": [1, 2]}}, ["testflavor"]
        )
        assert generic == {"status": "ok", "rendered": "QuickReplies", "count": 2}
    finally:
        rut._RENDER_UI_FLAVOR_PACKS.pop("testflavor", None)
