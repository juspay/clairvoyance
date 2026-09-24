from typing import Dict, List, cast

from app.ai.voice.agents.breeze_buddy.assist.commerce.skeleton import COMMERCE_V2
from app.ai.voice.agents.breeze_buddy.assist.platforms import registry as platforms
from app.ai.voice.agents.breeze_buddy.assist.verticals import registry


def test_commerce_is_the_default_and_the_only_vertical_today():
    assert registry.for_request(None).id == "commerce"
    assert registry.for_request("commerce").id == "commerce"
    assert registry.resolve("commerce").skeleton is COMMERCE_V2
    try:
        registry.for_request("booking")
    except KeyError:
        pass
    else:
        raise AssertionError("unknown vertical resolved")


def test_a_shopify_store_implies_commerce_and_a_plain_site_does_not():
    assert platforms.resolve("shopify").vertical == "commerce"
    assert platforms.resolve("generic").vertical is None


def test_commerce_vertical_supplies_what_the_service_used_to_hardcode():
    commerce = registry.resolve("commerce")
    assert commerce.blueprint_name == "buddy-assist-default"
    block = commerce.brand_block("Acme", "Acme Co", "Sells widgets.")
    assert (
        block.startswith("## Brand identity")
        and "{shop_url}" in block
        and "Sells widgets." in block
    )
    assert "shop_url" in commerce.payload_schema("acme.example.com")
    assert commerce.secrets("acme.example.com") == {"shop_url": "acme.example.com"}
    assert "never invent facts" in commerce.research_prompt()
    assert "not been personalized" in commerce.unpersonalized_context()


class TestRemovingTheLastOne:
    """An emptied field has to clear its configuration entry.

    `widget_values` used to write a key only when it had something in it
    (`if replies:`), which reads as caution and behaves as a bug: a merchant
    who deleted their last greeting tile saved successfully and kept the
    tile, because the old value was still in `configurations` and nothing
    overwrote it. Absent means "not edited"; present-and-empty means "gone".
    """

    def _vertical(self):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.vertical import vertical

        return vertical

    def test_an_emptied_list_clears_the_config(self):
        values = self._vertical().widget_values(
            {"quick_replies": [], "greeting_tiles": [], "initial_greeting": [""]}
        )
        assert values["quick_replies"] == []
        assert values["greeting_tiles"] == []
        assert values["initial_greeting"] == ""

    def test_a_field_nobody_sent_is_left_alone(self):
        values = self._vertical().widget_values({"brand_line": ["Acme"]})
        assert "quick_replies" not in values
        assert "greeting_tiles" not in values
        assert "initial_greeting" not in values
        assert "render_ui" not in values

    def test_filled_fields_still_arrive_config_shaped(self):
        values = self._vertical().widget_values(
            {
                "initial_greeting": ["What are we training for?\nLet's gear you up."],
                "quick_replies": ["Find me a gift", "What's new?"],
                "greeting_tiles": ["Bras | Show me bras | https://cdn.test/a.jpg"],
            }
        )
        # `widget_values` is typed as config-shaped, not commerce-shaped —
        # that is the point of it — so the test names the shape it expects.
        greeting = cast(str, values["initial_greeting"])
        replies = cast(List[Dict[str, str]], values["quick_replies"])
        assert greeting.startswith("What are we training for?")
        assert [reply["label"] for reply in replies] == [
            "Find me a gift",
            "What's new?",
        ]
        assert values["greeting_tiles"] == [
            {
                "label": "Bras",
                "prompt": "Show me bras",
                "image_url": "https://cdn.test/a.jpg",
            }
        ]

    def test_dropping_the_last_trusted_url_clears_the_list(self):
        values = self._vertical().widget_values({"trusted_extra": []})
        assert values["render_ui"] == {"trusted_link_urls": []}
