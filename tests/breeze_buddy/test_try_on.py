"""Virtual try-on: the pure pieces of the Buddy Assist try-on path.

Covers the places a mistake would be expensive and silent — pricing a
generation, deciding what may be tried on, and counting a session's spend
— without a wallet, a Redis, or a provider to call.
"""

from types import SimpleNamespace

import pytest
from google.genai import types

from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.schemas import (
    ProductDetailP,
)
from app.ai.voice.agents.breeze_buddy.assist.commerce.ucp.try_on_policy import (
    is_signed_try_on_image,
    is_try_on_eligible,
    sign_try_on_image,
)
from app.api.routers.breeze_buddy.widget.handlers import _try_on_count
from app.services.breeze_buddy.try_on.client import (
    TryOnGenerationError,
    _is_allowed_image_url,
    generate_try_on_image,
)
from app.services.breeze_buddy.wallet.deduction import BILLING_RULES, TRY_ON_CREDITS


class _State:
    """Stand-in for the AgentSessionState row, which is just a data dict."""

    def __init__(self, data):
        self.data = data


class TestGarmentUrl:
    """We fetch the garment ourselves, so the host decides. A product image
    is on a merchant CDN; anything else could be our own network."""

    def test_a_merchant_cdn_is_fetched(self):
        assert _is_allowed_image_url("https://cdn.shopify.com/s/files/1/x.jpg")
        assert _is_allowed_image_url("https://shop.myshopify.com/cdn/shop/x.jpg")

    def test_anything_else_is_refused(self):
        assert not _is_allowed_image_url("http://cdn.shopify.com/x.jpg")
        assert not _is_allowed_image_url("https://evil.example/x.jpg")
        assert not _is_allowed_image_url("https://169.254.169.254/meta-data")
        assert not _is_allowed_image_url("file:///etc/passwd")


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

    def test_whole_words_only(self):
        # "swatch" is not a watch; "matte" is not a mat.
        assert is_try_on_eligible(
            {"title": "Cotton Shirt", "product_type": "Swatch Matte"}
        )
        assert not is_try_on_eligible({"title": "Yoga Mat"})
        # A named garment outranks a word that only names a print or a pun.
        assert is_try_on_eligible({"title": "Perfume Print Tee"})
        assert is_try_on_eligible({"title": "Soul Mates Couple Tee"})

    def test_plurals_and_compound_names_are_eligible(self):
        assert is_try_on_eligible({"title": "Floral Maxi", "product_type": "Dresses"})
        assert is_try_on_eligible({"title": "Chronograph", "product_type": "Watches"})
        assert is_try_on_eligible({"title": "Classic Sweatshirt"})
        assert is_try_on_eligible({"title": "Graphic Tshirt"})
        assert is_try_on_eligible({"title": "Blue Swimsuit"})

    def test_garment_details_do_not_refuse_a_garment(self):
        assert is_try_on_eligible({"title": "Bottle Green Cotton Shirt"})
        assert is_try_on_eligible({"title": "Band Collar Linen Shirt"})
        assert is_try_on_eligible({"title": "Spaghetti Strap Top"})

    def test_things_named_after_a_garment_are_not(self):
        assert not is_try_on_eligible({"title": "Shoe Rack"})
        assert not is_try_on_eligible({"title": "Watch Box"})
        assert not is_try_on_eligible({"title": "Leather Watch Straps"})
        assert not is_try_on_eligible({"title": "Tie Clip"})

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

    def test_the_fitted_image_is_signed_as_the_widget_sends_it(self):
        product = self._detail(
            media=[{"url": "https://cdn.shopify.com/s/files/1/x/shirt.jpg"}]
        )
        wire = product.model_dump(mode="json")
        src = wire["images"][0]["src"]
        assert is_signed_try_on_image(src, wire["try_on_image_token"])

    def test_nothing_is_signed_without_eligibility_or_an_image(self):
        socks = self._detail(
            title="Crew Socks 3-Pack",
            media=[{"url": "https://cdn.shopify.com/s/files/1/x/socks.jpg"}],
        )
        assert socks.try_on_image_token is None
        assert self._detail().try_on_image_token is None


class TestImageSignature:
    """The try-on route fits only an image the server offered, so a client
    cannot spend credits on any other picture."""

    def test_a_signed_image_is_accepted(self):
        url = "https://cdn.shopify.com/s/files/1/x/shirt.jpg"
        assert is_signed_try_on_image(url, sign_try_on_image(url))

    def test_a_non_ascii_signature_is_refused_not_raised(self):
        # The token is client input; compare_digest raises on non-ASCII str.
        assert not is_signed_try_on_image("https://cdn.shopify.com/x.jpg", "é")

    def test_another_image_or_no_signature_is_refused(self):
        url = "https://cdn.shopify.com/s/files/1/x/shirt.jpg"
        signature = sign_try_on_image(url)
        assert not is_signed_try_on_image(
            "https://cdn.shopify.com/s/files/1/x/mug.jpg", signature
        )
        assert not is_signed_try_on_image(url, "")


class _FakeReply:
    """A model reply that carries no image, with the reason that decides
    whether a retry could ever help."""

    def __init__(self, finish_reason):
        self.candidates = [
            SimpleNamespace(
                finish_reason=finish_reason, content=SimpleNamespace(parts=[])
            )
        ]
        self.prompt_feedback = None


class TestRefusedPhoto:
    """A safety refusal is about THIS photo, so a retry repeats it — the
    shopper must be told to change photos, not to try again."""

    async def _generate(self, monkeypatch, finish_reason):
        from app.services.breeze_buddy.try_on import client as provider

        async def fake_client():
            async def generate_content(**_kwargs):
                return _FakeReply(finish_reason)

            return SimpleNamespace(
                aio=SimpleNamespace(
                    models=SimpleNamespace(generate_content=generate_content)
                )
            )

        async def fake_garment(_url, _where):
            return types.Part.from_text(text="garment")

        monkeypatch.setattr(provider, "_vertex_client", fake_client)
        monkeypatch.setattr(provider, "_fetch_garment", fake_garment)
        with pytest.raises(TryOnGenerationError) as caught:
            await generate_try_on_image(
                merchant_domain="shop.myshopify.com",
                photo_bytes=b"x",
                photo_content_type="image/jpeg",
                garment_image_url="https://cdn.shopify.com/shirt.jpg",
                product_id="p1",
                request_id="r1",
            )
        return caught.value

    async def test_a_refusal_asks_for_a_different_photo(self, monkeypatch):
        error = await self._generate(monkeypatch, "IMAGE_SAFETY")
        assert error.code == "photo_rejected"
        assert "photo" in error.message.lower()
        assert "try again" not in error.message.lower()

    async def test_every_other_empty_reply_stays_a_retry(self, monkeypatch):
        error = await self._generate(monkeypatch, "STOP")
        assert error.code == "empty_result"
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


class _FakeRedis:
    def __init__(self):
        self.keys = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True

    async def get(self, key):
        return self.keys.get(key)

    async def delete(self, key):
        return self.keys.pop(key, None) is not None


class TestRequestClaim:
    """One request_id produces at most one image. Without the claim, a
    replay after the result cache expired generated a second image and
    the ledger's duplicate check then served it free."""

    @pytest.fixture(autouse=True)
    def redis(self, monkeypatch):
        from app.services.breeze_buddy.try_on import cache

        fake = _FakeRedis()

        async def get_redis_service():
            return fake

        async def timeout():
            return 120

        monkeypatch.setattr(cache, "get_redis_service", get_redis_service)
        monkeypatch.setattr(cache, "TRY_ON_GENERATION_TIMEOUT_SECONDS", timeout)
        return fake

    async def test_a_second_request_waits_while_the_first_runs(self):
        from app.services.breeze_buddy.try_on import claim_try_on_request

        assert await claim_try_on_request("s1", "r1") == "claimed"
        assert await claim_try_on_request("s1", "r1") == "running"
        # Scoped by session: another session's id is its own.
        assert await claim_try_on_request("s2", "r1") == "claimed"

    async def test_a_finished_id_is_never_generated_again(self):
        from app.services.breeze_buddy.try_on import (
            claim_try_on_request,
            finish_try_on_request,
        )

        await claim_try_on_request("s1", "r1")
        await finish_try_on_request("s1", "r1")
        assert await claim_try_on_request("s1", "r1") == "done"

    async def test_a_failed_generation_frees_the_id_for_a_retry(self):
        from app.services.breeze_buddy.try_on import (
            claim_try_on_request,
            release_try_on_request,
        )

        await claim_try_on_request("s1", "r1")
        await release_try_on_request("s1", "r1")
        assert await claim_try_on_request("s1", "r1") == "claimed"
