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
