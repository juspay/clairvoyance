"""Unit pins for the product media resolver (assist/commerce/media.py):
handle-URL derivation + the UCP-wins / fail-open decision surface. The
live storefront fetch is covered by the wire check, not here."""

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.media import (
    _shopify_handle_url,
    resolve_product_media,
)


class TestHandleUrl:
    def test_plain_product_url(self):
        assert (
            _shopify_handle_url("https://shop.example/products/flowmesh-leggings")
            == "https://shop.example/products/flowmesh-leggings.json"
        )

    def test_url_with_collection_prefix_and_query(self):
        assert (
            _shopify_handle_url(
                "https://shop.example/collections/sale/products/tee?variant=1"
            )
            == "https://shop.example/products/tee.json"
        )

    def test_non_product_paths_and_junk(self):
        assert _shopify_handle_url("https://shop.example/pages/contact") is None
        assert _shopify_handle_url("https://shop.example/products/") is None
        assert _shopify_handle_url("not a url") is None


class TestResolveDecision:
    @pytest.mark.asyncio
    async def test_rich_ucp_media_is_a_noop(self):
        product = {
            "url": "https://shop.example/products/x",
            "media": [{"url": "a"}, {"url": "b"}],
        }
        # session=None would crash any fetch attempt — proving no fetch runs.
        assert await resolve_product_media(None, product) is None

    @pytest.mark.asyncio
    async def test_no_url_or_no_session_fails_open(self):
        assert await resolve_product_media(None, {"media": [{"url": "a"}]}) is None
        assert (
            await resolve_product_media(
                None, {"url": "https://shop.example/products/x", "media": []}
            )
            is None
        )
