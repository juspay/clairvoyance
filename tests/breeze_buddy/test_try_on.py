"""Virtual try-on: the pure pieces of the Buddy Assist try-on path.

Covers the three places a mistake would be expensive and silent — pricing
a generation, reading the provider's reply, and counting a session's spend
— without a wallet, a Redis, or a nautilus to talk to.
"""

import pytest

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
    ProductDetailP,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.try_on_policy import (
    is_try_on_eligible,
    try_on_mode,
)
from app.ai.voice.agents.breeze_buddy.assist.platforms.shopify.tenancy import (
    assist_merchant_domain,
    assist_tenant,
)
from app.api.routers.breeze_buddy.widget.handlers import _try_on_count
from app.services.breeze_buddy.try_on.client import (
    TryOnGenerationError,
    _data_uri,
    _first_image,
    generate_try_on_image,
)
from app.services.breeze_buddy.wallet.deduction import BILLING_RULES, TRY_ON_CREDITS


class _State:
    """Stand-in for the AgentSessionState row, which is just a data dict."""

    def __init__(self, data):
        self.data = data


class TestShopDomain:
    """nautilus finds the shop by its plain domain, never the tenant id."""

    @pytest.mark.parametrize("host_app", ["buddy-assist", "breeze-buddy"])
    def test_every_host_app_sends_the_plain_domain(self, host_app):
        reseller_id, merchant_id = assist_tenant(host_app, "shop.myshopify.com")
        assert assist_merchant_domain(reseller_id, merchant_id) == (
            "shop.myshopify.com"
        )

    def test_a_plain_domain_that_starts_with_assist_is_kept(self):
        assert assist_merchant_domain("BB_SHOPIFY", "assist-co.myshopify.com") == (
            "assist-co.myshopify.com"
        )


class TestPricing:
    def test_try_on_is_a_flat_price_per_image(self):
        # Priced in the rule, like chat_turn's literal 1 — the call site
        # passes no credits, so it cannot disagree with the wallet check.
        assert BILLING_RULES["try_on"]() == TRY_ON_CREDITS

    def test_the_price_is_never_free(self):
        # A zero price would silently give away paid provider calls.
        assert TRY_ON_CREDITS > 0

    def test_chat_and_voice_are_untouched(self):
        assert BILLING_RULES["chat_turn"]() == 1
        assert BILLING_RULES["voice_call"](duration_seconds=45) == 2


class TestProviderReply:
    def test_prefers_a_hosted_url(self):
        payload = {
            "results": [{"imageUrl": "https://cdn/out.png", "imageBase64": "AAAA"}]
        }
        assert _first_image(payload) == "https://cdn/out.png"

    def test_builds_a_data_uri_from_bare_base64(self):
        payload = {
            "results": [{"imageBase64": "AAAA", "metadata": {"mimeType": "image/jpeg"}}]
        }
        assert _first_image(payload) == "data:image/jpeg;base64,AAAA"

    def test_defaults_the_mime_type(self):
        assert (
            _first_image({"results": [{"imageBase64": "AAAA"}]})
            == "data:image/png;base64,AAAA"
        )

    def test_passes_through_an_existing_data_uri(self):
        uri = "data:image/png;base64,AAAA"
        assert _first_image({"results": [{"imageBase64": uri}]}) == uri

    def test_no_image_is_none_not_an_exception(self):
        # The caller turns None into a shopper-safe error and charges nothing.
        assert _first_image({}) is None
        assert _first_image({"results": []}) is None
        assert _first_image({"results": [{}]}) is None
        assert _first_image({"results": ["not a dict"]}) is None


class TestPhotoEncoding:
    def test_uses_the_uploaded_content_type(self):
        assert _data_uri(b"x", "image/png").startswith("data:image/png;base64,")

    def test_falls_back_to_jpeg_for_anything_else(self):
        # Browsers send application/octet-stream often enough that trusting
        # the part's type unchecked would fail nautilus's MIME allowlist.
        assert _data_uri(b"x", None).startswith("data:image/jpeg;base64,")
        assert _data_uri(b"x", "application/octet-stream").startswith(
            "data:image/jpeg;base64,"
        )


class TestSessionCounter:
    def test_counts_previous_generations(self):
        assert _try_on_count(_State({"try_on": {"count": 3}})) == 3

    def test_absent_or_malformed_state_counts_as_zero(self):
        assert _try_on_count(None) == 0
        assert _try_on_count(_State(None)) == 0
        assert _try_on_count(_State({})) == 0
        assert _try_on_count(_State({"try_on": "nope"})) == 0
        assert _try_on_count(_State({"try_on": {"count": "3"}})) == 0
        assert _try_on_count(_State({"try_on": {"count": -1}})) == 0

    def test_ignores_other_session_state(self):
        # cart_id and friends share this row; the cap must not read them.
        assert (
            _try_on_count(_State({"cart_id": "gid://…", "try_on": {"count": 1}})) == 1
        )


class TestEligibility:
    """A generation the provider cannot fit still costs a credit, so the
    gate fails closed on anything it does not recognise."""

    def test_garments_are_eligible(self):
        assert is_try_on_eligible({"title": "LayerLite Crop Top Pink"})
        assert is_try_on_eligible({"title": "Stride Shorts - Sky Blue"})
        assert is_try_on_eligible({"title": "All Day Muse Playsuit"})
        assert is_try_on_eligible({"title": "Antonello Cream Formal Cotton Shirt"})
        assert is_try_on_eligible({"title": "Sleeveless Jodhpuri"})
        assert is_try_on_eligible({"title": "Tailored Fit Chinos"})

    def test_attribute_tags_cannot_veto_a_garment(self):
        """Live regression: Zodiac ships ~120 attribute tags per shirt, one
        of which is "Swatch". Substring matching read that as "watch" and
        hid try-on on every shirt in the store. Identity comes from the
        title and product type; tags describe, they do not name."""
        shirt = {
            "title": "Antonello Cream Solid Full Sleeve Classic Formal Cotton Shirt",
            "product_type": "Shirts",
            "tags": ["Swatch", "Fabric : Matte", "Belt Loops : No", "Waistband : None"],
        }
        assert is_try_on_eligible(shirt)
        assert try_on_mode(shirt) == "upper_body"

    def test_whole_words_only(self):
        # "swatch" is not a watch; "matte" is not a mat.
        assert is_try_on_eligible(
            {"title": "Cotton Shirt", "product_type": "Swatch Matte"}
        )
        assert not is_try_on_eligible({"title": "Yoga Mat"})

    def test_wearable_accessories_are_eligible(self):
        assert is_try_on_eligible({"title": "Classic Leather Watch"})
        assert is_try_on_eligible({"title": "Brown Plain Leather Belt"})
        assert is_try_on_eligible({"title": "Navy Silk Tie"})
        assert is_try_on_eligible({"title": "Canvas Backpack"})

    def test_what_no_provider_can_show_is_not(self):
        assert not is_try_on_eligible({"title": "Crew Socks 3-Pack"})
        assert not is_try_on_eligible({"title": "Leather Wallet"})
        assert not is_try_on_eligible({"title": "Black Stone Cufflinks"})
        assert not is_try_on_eligible({"title": "Apple Watch Leather Band"})
        assert not is_try_on_eligible({"title": "Gift Card"})

    def test_unknown_vocabulary_is_refused(self):
        assert not is_try_on_eligible({"title": "Mystery Box"})
        assert not is_try_on_eligible({"title": ""})
        assert not is_try_on_eligible({})
        assert not is_try_on_eligible(None)

    def test_product_type_counts_as_vocabulary(self):
        assert is_try_on_eligible({"title": "Muse", "product_type": "Jumpsuit"})
        assert not is_try_on_eligible({"title": "Muse", "product_type": "Accessories"})

    def test_tags_alone_never_qualify_a_product(self):
        # A tag saying "top" on an unnamed product is not enough to spend
        # a credit on.
        assert not is_try_on_eligible(
            {"title": "Mystery Item", "tags": ["top", "shirt"]}
        )


class TestMode:
    def test_region_follows_the_product(self):
        assert try_on_mode({"title": "Stride Shorts"}) == "lower_body"
        assert try_on_mode({"title": "Crop Top"}) == "upper_body"
        assert try_on_mode({"title": "All Day Muse Playsuit"}) == "full_body"

    def test_a_set_is_fitted_on_the_lower_half(self):
        assert try_on_mode({"title": "Crop Top and Shorts Co-ord"}) == "lower_body"

    def test_unknown_products_default_to_full_body(self):
        assert try_on_mode(None) == "full_body"
        assert try_on_mode({"title": "Mystery Box"}) == "full_body"


class TestProjectionStamp:
    """The flags must be stamped by the PROJECTION, not by the
    post-hydration hook.

    Regression: they were first stamped in render_ui's finalize_hydrated,
    which only runs for LLM-authored render_ui calls. The detail overlay
    is opened by the DIRECT view_product intent, whose resolver never
    calls that hook — so every product arrived ineligible and the button
    never appeared. Both paths run this projection.
    """

    def _detail(self, **overrides):
        payload = {
            "id": "gid://shopify/Product/1",
            "title": "Barboni White Solid Full Sleeve Classic Formal Cotton Shirt",
            "price_range": {"min": {"amount": 4213, "currency": "INR"}},
            "media": [],
            "variants": [],
        }
        payload.update(overrides)
        return ProductDetailP.model_validate(payload)

    def test_a_garment_is_stamped_eligible(self):
        product = self._detail()
        assert product.try_on_eligible is True
        assert product.try_on_mode == "upper_body"

    def test_an_unshowable_product_is_not(self):
        product = self._detail(title="Crew Socks 3-Pack")
        assert product.try_on_eligible is False

    def test_the_flags_survive_the_ucp_lift(self):
        # The lift rewrites description/price/images; the stamp must not
        # be lost among those rewrites.
        product = self._detail(
            description={"html": "<p>A classic formal white shirt.</p>"},
            media=[{"url": "https://cdn.shopify.com/s/files/1/x/shirt.jpg"}],
        )
        assert product.try_on_eligible is True
        assert product.description == "A classic formal white shirt."
        assert len(product.images) == 1


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = "refused"

    def json(self):
        return {}


class _FakeClient:
    """Enough of httpx.AsyncClient for the status mapping below."""

    def __init__(self, status_code):
        self._status_code = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def post(self, *_args, **_kwargs):
        return _FakeResponse(self._status_code)


class TestRefusedPhoto:
    """422 means the provider refused THIS photo, so a retry repeats it —
    the shopper must be told to change photos, not to try again."""

    @pytest.fixture(autouse=True)
    def _stub_nautilus(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.breeze_buddy.try_on.client.NAUTILUS_TRY_ON_SECRET",
            "test-secret",
        )

    async def _generate(self, monkeypatch, status_code):
        monkeypatch.setattr(
            "app.services.breeze_buddy.try_on.client.create_http_client",
            lambda **_: _FakeClient(status_code),
        )
        with pytest.raises(TryOnGenerationError) as caught:
            await generate_try_on_image(
                merchant_domain="shop.myshopify.com",
                photo_bytes=b"x",
                photo_content_type="image/jpeg",
                garment_image_url="https://cdn.shopify.com/shirt.jpg",
                mode="upper_body",
                product_id="p1",
                request_id="r1",
            )
        return caught.value

    async def test_a_refusal_asks_for_a_different_photo(self, monkeypatch):
        error = await self._generate(monkeypatch, 422)
        assert error.code == "photo_rejected"
        assert "photo" in error.message.lower()
        assert "try again" not in error.message.lower()

    async def test_every_other_failure_stays_a_retry(self, monkeypatch):
        error = await self._generate(monkeypatch, 500)
        assert error.code == "http_500"
        assert "try again" in error.message.lower()


class _Configurations:
    def __init__(self, **fields):
        for k, v in fields.items():
            setattr(self, k, v)


class _Template:
    def __init__(self, configurations=None):
        self.configurations = configurations


class TestEntitlement:
    """The per-merchant switch. Try-on spends credits, so every unknown
    must read as "no" — unlike voice, which defaults on for legacy
    templates."""

    @staticmethod
    def _enabled(template):
        from app.api.routers.breeze_buddy.widget.handlers import (
            _template_try_on_enabled,
        )

        return _template_try_on_enabled(template)

    def test_a_template_that_opts_in_is_enabled(self):
        assert self._enabled(_Template(_Configurations(enable_try_on=True))) is True

    def test_a_template_that_opts_out_is_not(self):
        assert self._enabled(_Template(_Configurations(enable_try_on=False))) is False

    def test_a_template_that_never_heard_of_try_on_is_not(self):
        assert self._enabled(_Template(_Configurations())) is False

    def test_no_configurations_at_all_is_not(self):
        assert self._enabled(_Template(None)) is False

    def test_an_unloadable_template_is_not(self):
        # get_template_by_id_cached returns None when the row is gone.
        assert self._enabled(None) is False


class TestRequestIdShape:
    """The key joins a VARCHAR(255) ledger column. An over-long value used
    to fail the INSERT after the image existed, serving it free."""

    @staticmethod
    def _ok(value):
        from app.api.routers.breeze_buddy.widget.handlers import _VALID_REQUEST_ID

        return bool(_VALID_REQUEST_ID.fullmatch(value))

    def test_a_uuid_is_accepted(self):
        assert self._ok("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

    def test_the_widget_fallback_shape_is_accepted(self):
        assert self._ok("m1x2y3-ab12cd34")

    def test_an_overlong_key_is_refused(self):
        # 36-char session id + ':' + this must stay inside VARCHAR(255).
        assert not self._ok("x" * 65)

    def test_punctuation_that_could_forge_a_log_line_is_refused(self):
        assert not self._ok("abc\ndef")
        assert not self._ok("a:b")
        assert not self._ok("")
