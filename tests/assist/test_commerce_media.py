"""Unit pins for the product-media seam.

Two layers, tested where each one lives:

* ``ucp/media.py`` — the protocol-level decision (UCP media wins when it
  already carries a gallery; otherwise ask the connectors) and fail-open.
* ``connectors/shopify/media.py`` — handle-URL derivation, the only
  Shopify knowledge in the flavor. The live storefront fetch is covered by
  the wire check, not here.
"""

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors.shopify.media import (
    _handle_url,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.media import (
    resolve_product_media,
)


class TestShopifyHandleUrl:
    def test_plain_product_url(self):
        assert (
            _handle_url("https://shop.example/products/flowmesh-leggings")
            == "https://shop.example/products/flowmesh-leggings.json"
        )

    def test_url_with_collection_prefix_and_query(self):
        assert (
            _handle_url("https://shop.example/collections/sale/products/tee?variant=1")
            == "https://shop.example/products/tee.json"
        )

    def test_non_product_paths_and_junk(self):
        assert _handle_url("https://shop.example/pages/contact") is None
        assert _handle_url("https://shop.example/products/") is None
        assert _handle_url("not a url") is None


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


class TestProtocolLayerHasNoConnector:
    """The UCP layer must not name a platform — the whole point of the
    connectors/ split. A regression here means platform code crept back
    into the protocol module."""

    def test_ucp_media_module_is_platform_blind(self):
        from pathlib import Path

        import app.ai.voice.agents.breeze_buddy.assist.commerce.ucp as ucp_pkg

        for path in Path(ucp_pkg.__file__).parent.glob("*.py"):
            source = path.read_text().lower()
            assert (
                "shopify" not in source
            ), f"{path.name} names a platform; it belongs in connectors/"


class TestOutboundFetchIsGated:
    """``product.url`` is upstream-controlled data driving a request the
    SERVER makes, so the host is gated, not just the path shape."""

    def test_ip_literal_hosts_that_could_reach_infra_are_refused(self):
        for hostile in (
            "http://169.254.169.254/products/x",  # cloud metadata endpoint
            "https://127.0.0.1/products/x",
            "https://10.0.0.5/products/x",
            "https://192.168.1.1/products/x",
            "https://[::1]/products/x",
            "https://0.0.0.0/products/x",
        ):
            assert _handle_url(hostile) is None, hostile

    def test_public_ip_literal_is_allowed(self):
        assert (
            _handle_url("https://93.184.216.34/products/x")
            == "https://93.184.216.34/products/x.json"
        )

    def test_userinfo_is_stripped_not_forwarded_or_logged(self):
        built = _handle_url("https://user:secret@shop.example/products/x")
        assert built == "https://shop.example/products/x.json"
        assert "secret" not in built

    def test_explicit_non_https_port_is_refused(self):
        assert _handle_url("https://shop.example:8000/products/x") is None
        assert _handle_url("https://shop.example:9200/products/x") is None
        # The default port, stated explicitly, is still fine.
        assert (
            _handle_url("https://shop.example:443/products/x")
            == "https://shop.example/products/x.json"
        )

    @pytest.mark.asyncio
    async def test_name_resolving_to_a_private_address_is_not_fetched(
        self, monkeypatch
    ):
        from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors.shopify import (  # noqa: E501
            media as shopify_media,
        )

        class _Session:
            def get(self, *_a, **_k):
                raise AssertionError("must not fetch a private-resolving host")

        async def _private(_host):
            return False

        monkeypatch.setattr(shopify_media, "_host_resolves_public", _private)
        product = {"url": "https://evil.example/products/x", "media": []}
        assert await shopify_media.resolve_gallery(_Session(), product) is None

    @pytest.mark.asyncio
    async def test_redirects_are_not_followed(self, monkeypatch):
        """A 302 to an internal host would sidestep every check above."""
        from app.ai.voice.agents.breeze_buddy.assist.commerce.connectors.shopify import (  # noqa: E501
            media as shopify_media,
        )

        seen = {}

        class _Resp:
            status = 404

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def json(self, **_k):
                return {}

        class _Session:
            def get(self, url, **kwargs):
                seen.update(url=url, **kwargs)
                return _Resp()

        async def _public(_host):
            return True

        monkeypatch.setattr(shopify_media, "_host_resolves_public", _public)
        await shopify_media.resolve_gallery(
            _Session(), {"url": "https://shop.example/products/x", "media": []}
        )
        assert seen["allow_redirects"] is False
